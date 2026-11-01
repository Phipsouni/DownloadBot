import json
import tempfile
import unittest
import zipfile
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

import test_auto_delivery as auto_cases
from test_downloads import FakeYDL
import download_pipeline as pipeline
from music_sources import MusicSourceError
from soundcloud_fallback import (find_soundcloud_audio, soundcloud_plan, read_soundcloud_info,
                                 SOURCE_NOTE, same_version, comparable_title, split_combined_artists)


class SoundCloudMatchingTests(unittest.TestCase):
    def test_matching_requires_artist_title_duration_and_marks_the_new_source(self):
        source = {'title':'Left, Right (feat. Mustard)','artists':['YG'],'duration':232.966}
        client = Mock()
        client.search.return_value = [
            {'title':source['title'],'artists':[{'name':'Other'}],'duration_seconds':233,'videoId':'abcdefghijk'},
            {'title':source['title']+' (Remix)','artists':[{'name':'YG'}],'duration_seconds':233,'videoId':'abcdefghijk'},
            {'title':source['title'],'artists':[{'name':'YG'}],'duration_seconds':30,'videoId':'abcdefghijk'},
            {'title':source['title'],'artists':[{'name':'YG'}],'duration_seconds':233,'videoId':'validid1234'},
        ]
        url, metadata = find_soundcloud_audio(source,client)
        self.assertTrue(url.endswith('validid1234'))
        self.assertEqual(metadata['artist'],'YG')
        self.assertIn('YouTube Music',metadata['_source_note'])
        self.assertIn('не lossless',metadata['_source_note'])
        client.search.return_value = client.search.return_value[:3]
        with self.assertRaises(MusicSourceError):
            find_soundcloud_audio(source,client)

    def test_missing_or_invalid_duration_prevents_unverified_substitution(self):
        client = Mock()
        for duration in (None,0,float('nan'),-1):
            with self.assertRaises(MusicSourceError):
                find_soundcloud_audio({'title':'Track','artist':'Artist','duration':duration},client)
        client.search.assert_not_called()

    def test_remix_clean_and_radio_edit_versions_are_not_interchangeable(self):
        self.assertFalse(same_version('Track (Remix)','Track'))
        self.assertFalse(same_version('Track','Track (Radio Edit)'))
        self.assertFalse(same_version('Track','Track (Clean)'))
        self.assertFalse(same_version('Track (Slowed + Reverb)','Track (Slowed)'))
        self.assertTrue(same_version('Track (Remix)','Track - Remix'))

    def test_hollywood_matches_despite_combined_artists_and_feat_title(self):
        source={'title':'HOLLYWOOD feat shoreline mafia','artist':'YG， Shoreline Mafia',
                'artists':['YG, Shoreline Mafia'],'uploader':'YG','duration':180.32}
        client=Mock()
        client.search.return_value=[
            {'title':'HOLLYWOOD','artists':[{'name':'YG'},{'name':'Shoreline Mafia'}],
             'duration_seconds':181,'videoId':'uhGWytRbsnQ'}]
        url,metadata=find_soundcloud_audio(source,client)
        self.assertTrue(url.endswith('uhGWytRbsnQ'))
        self.assertEqual(metadata['artist'],'YG, Shoreline Mafia')
        self.assertEqual(metadata['track'],source['title'])
        self.assertEqual(client.search.call_count,1)
        self.assertEqual(client.search.call_args.kwargs['filter'],'songs')

    def test_missing_song_can_match_a_video_with_verified_credits_and_duration(self):
        source={'title':'Track feat Guest','artist':'Artist','duration':180}
        client=Mock()
        video={'title':'Artist - Track (Official Audio) ft. Guest',
               'artists':[{'name':'Artist'}],'duration_seconds':181,'videoId':'validid1234'}
        client.search.side_effect=lambda *args,**kwargs: [] if kwargs['filter']=='songs' else [video]
        url,_=find_soundcloud_audio(source,client)
        self.assertTrue(url.endswith('validid1234'))
        self.assertEqual([call.kwargs['filter'] for call in client.search.call_args_list],['songs','videos'])

    def test_same_title_with_different_featured_artist_is_rejected(self):
        source={'title':'HOLLYWOOD feat shoreline mafia','artists':['YG, Shoreline Mafia'],
                'uploader':'YG','duration':180.32}
        client=Mock()
        client.search.return_value=[{'title':'HOLLYWOOD','artists':[{'name':'YG'},{'name':'Other Guest'}],
                                    'duration_seconds':181,'videoId':'validid1234'}]
        with self.assertRaises(MusicSourceError):
            find_soundcloud_audio(source,client)

    def test_artist_names_with_legitimate_commas_are_not_split(self):
        self.assertEqual(split_combined_artists(['Tyler, the Creator'],'Tyler, the Creator'),
                         ['Tyler, the Creator'])
        self.assertEqual(comparable_title('YG - HOLLYWOOD (Official Audio) ft. Shoreline Mafia',['YG'])[0],
                         'HOLLYWOOD')

    def test_longer_music_video_is_not_chosen_instead_of_a_matching_audio_version(self):
        source={'title':'HOLLYWOOD feat shoreline mafia','artists':['YG, Shoreline Mafia'],
                'uploader':'YG','duration':180.32}
        client=Mock()
        client.search.return_value=[{'title':'HOLLYWOOD','artists':[{'name':'YG'},{'name':'Shoreline Mafia'}],
                                    'duration_seconds':194,'videoId':'0i4UlhUkn5w'}]
        with self.assertRaises(MusicSourceError):
            find_soundcloud_audio(source,client)

    def test_playlist_plan_keeps_order_without_storing_stream_urls(self):
        info = {'id':'set','title':'Альбом','entries':[
            {'id':'1','url':'https://soundcloud.com/yg400/one'},
            {'id':'2','url':'https://api-v2.soundcloud.com/tracks/2'},
        ],'formats':[{'url':'https://cdn.example.com/private-signed-stream'}]}
        plan = soundcloud_plan(info,'https://soundcloud.com/yg400/sets/album')
        self.assertEqual([item['playlist_index'] for item in plan],[1,2])
        self.assertTrue(all(item['album']=='Альбом' for item in plan))
        self.assertNotIn('private-signed-stream',json.dumps(plan))

    def test_metadata_read_can_return_drm_info_without_a_download(self):
        recorded = []
        class Metadata:
            def __init__(self,options):
                self.options=options
                recorded.append(options)
            def __enter__(self): return self
            def __exit__(self,*args): return False
            def extract_info(self,url,download):
                assert not download
                self.options['logger'].warning('This video is DRM protected')
                return {'title':'Protected','artist':'YG','duration':180,'formats':[]}
        info, protected = read_soundcloud_info('url',[('anonymous',{})],Mock(),{},Metadata)
        self.assertTrue(protected)
        self.assertEqual(info['title'],'Protected')
        self.assertTrue(recorded[0]['ignore_no_formats_error'])

    def test_authorized_native_format_is_preferred_over_anonymous_drm_metadata(self):
        calls = []
        class Metadata:
            def __init__(self,options): self.options=options
            def __enter__(self): return self
            def __exit__(self,*args): return False
            def extract_info(self,url,download):
                calls.append(url)
                if len(calls)==1:
                    self.options['logger'].warning('This video is DRM protected')
                    return {'title':'Track','formats':[]}
                return {'title':'Track','formats':[{'ext':'flac'}]}
        info, protected = read_soundcloud_info('url',[('anonymous',{}),('authorized',{})],Mock(),{},Metadata)
        self.assertFalse(protected)
        self.assertEqual(info['formats'][0]['ext'],'flac')


class FallbackDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        auto_cases.AutoDeliveryTests.setUpClass()

    def setUp(self):
        self.fixture = auto_cases.AutoDeliveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.app,self.bot = self.fixture.app,self.fixture.bot
        self.client = Mock()
        self.client.search.return_value = [{'title':'Protected','artists':[{'name':'YG'}],
                                            'duration_seconds':180,'videoId':'validid1234'}]
        patch.object(self.app,'music_search_client',return_value=nullcontext(self.client)).start()
        self.paths = []
        self.urls = []
        fixture = self
        class Routed(FakeYDL):
            def extract_info(self,url,download=True):
                fixture.urls.append(url)
                return fixture.route(self,url)
        patch.object(pipeline,'YoutubeDL',Routed).start()
        patch.object(self.app,'read_soundcloud_info',return_value=(
            {'title':'Protected','artist':'YG','duration':180},True)).start()

    def source(self,name='Protected',protected=True):
        return {'_soundcloud':True,'_drm':protected,
                'source_url':f'https://soundcloud.com/yg400/{name.lower()}',
                'title':name,'artist':'YG','artists':['YG'],'duration':180,
                'album':'Альбом','playlist_index':1,'playlist_id':'set'}

    def route(self,ydl,url):
        if url.startswith('https://soundcloud.com/') and url.endswith('/protected'):
            ydl.params['logger'].error('This video is DRM protected')
            return None
        title = 'Protected' if 'youtube.com' in url else url.rsplit('/',1)[-1].title()
        path = Path(ydl.params['paths']['home'])/'track.m4a'
        path.write_bytes(b'audio')
        self.paths.append(path)
        info = {'id':title,'title':title,'artist':'YG','filepath':str(path)}
        ydl.processor.callback(info)
        return info

    def run_request(self,entries,delivery='auto'):
        self.app.download_content('https://soundcloud.com/yg400/sets/album',2,'audio',
                                  delivery,'original',2,entries)
        self.assertEqual(list(self.fixture.downloads.iterdir()),[])

    def test_protected_single_track_is_downloaded_from_youtube_with_caption(self):
        self.run_request([self.source()])
        self.assertEqual(self.urls,['https://www.youtube.com/watch?v=validid1234'])
        self.assertEqual(self.bot.deliveries[0][1],'YG — Protected.m4a')
        self.assertIn(SOURCE_NOTE,self.bot.deliveries[0][2]['caption'])
        self.fixture.cloud.assert_not_called()
        self.assertIn('Из-за DRM скачано с YouTube Music: 1',self.bot.statuses[-1])

    def test_album_keeps_order_when_middle_track_becomes_drm(self):
        self.run_request([self.source('First',False),self.source('Protected',False),self.source('Third',False)])
        self.assertEqual(self.bot.sent,['First','Protected','Third'])
        self.assertNotIn('YouTube Music',self.bot.deliveries[0][2]['caption'])
        self.assertIn('YouTube Music',self.bot.deliveries[1][2]['caption'])
        self.assertNotIn('YouTube Music',self.bot.deliveries[2][2]['caption'])
        self.assertEqual(len(self.urls),4)

    def test_no_match_skips_substitution_and_continues_album(self):
        self.client.search.return_value = []
        self.run_request([self.source(),self.source('Next',False)])
        self.assertEqual(self.bot.sent,['Next'])
        self.assertFalse(any('youtube.com' in url for url in self.urls))
        self.assertIn('Не найдено точное соответствие',self.bot.statuses[-1])

    def test_non_drm_network_error_does_not_trigger_a_search(self):
        self.route = lambda ydl,url: ydl.params['logger'].error('HTTP Error 403: unavailable')
        self.run_request([self.source('Unavailable',False)])
        self.client.search.assert_not_called()
        self.assertFalse(self.bot.deliveries)

    def test_youtube_download_failure_does_not_claim_success_or_leave_files(self):
        def fail(ydl,url):
            (Path(ydl.params['paths']['home'])/'partial.part').write_bytes(b'partial')
            ydl.params['logger'].error('YouTube download failed')
            return None
        self.route=fail
        self.run_request([self.source()])
        self.assertFalse(self.bot.deliveries)
        self.assertIn('YouTube download failed',self.bot.statuses[-1])
        self.assertNotIn('Из-за DRM скачано',self.bot.statuses[-1])

    def test_large_substitution_link_names_youtube_source_and_keeps_user_quota(self):
        patch.object(self.app,'MAX_FILE_SIZE_BYTES',1).start()
        self.run_request([self.source()])
        self.assertIn('YouTube Music',self.bot.cloud_messages[0][0])
        self.assertIn('не lossless',self.bot.cloud_messages[0][0])
        self.assertEqual(self.fixture.upload.call_args.kwargs['user_id'],'2')
        self.assertEqual(self.fixture.upload.call_args.kwargs['user_limit_bytes'],2_000_000_000)

    def test_zip_contains_utf8_source_manifest_and_replacement_names(self):
        manifest=[]
        original=self.fixture.storage.upload
        def upload(path,*args,**kwargs):
            with zipfile.ZipFile(path) as archive:
                manifest.append(archive.read('ИСТОЧНИКИ.txt').decode('utf-8'))
            return original(path,*args,**kwargs)
        patch.object(self.fixture.storage,'upload',side_effect=upload).start()
        self.run_request([self.source('First',False),self.source()],delivery='zip')
        self.assertIn('YG — Protected',manifest[0])
        self.assertIn('не lossless',manifest[0])
        self.assertNotIn('YG — First',manifest[0])
        self.assertIn('YG — Protected',self.bot.cloud_messages[0][0])
        self.assertEqual(len(self.fixture.storage.uploaded),1)


if __name__ == '__main__':
    unittest.main()
