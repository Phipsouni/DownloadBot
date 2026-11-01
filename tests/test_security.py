import importlib.util
import io
import json
import logging
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
import telebot
from curl_cffi import CurlOpt
from curl_cffi.requests import Session as CurlSession
from yt_dlp.utils import DownloadError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import network_safety as net
import secret_safety as secrets
import security_check as publication
from access_safety import RateGate, telegram_user_id


def dns(ip='93.184.215.14', port=443):
    family = socket.AF_INET6 if ':' in ip else socket.AF_INET
    address = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', address)]


class NetworkSafetyTests(unittest.TestCase):
    def test_local_reserved_and_ambiguous_urls_are_blocked(self):
        cases = [
            'http://127.0.0.1/', 'http://10.0.0.1/', 'http://172.16.0.1/',
            'http://192.168.1.1/', 'http://169.254.169.254/latest/meta-data/',
            'http://100.64.0.1/', 'http://0.0.0.0/', 'http://224.0.0.1/',
            'http://[::1]/', 'http://[fc00::1]/', 'http://[fe80::1]/',
            'http://[::ffff:127.0.0.1]/', 'http://[64:ff9b::7f00:1]/',
            'http://[2002:7f00:1::]/', 'http://localhost./', 'http://router.local/',
            'https://youtube.com:8080/', 'https://user:password@youtube.com/',
            'https://youtube.com\\@127.0.0.1/', 'https://youtube.com/\nredirect',
            'file:///C:/Windows/win.ini', 'ftp://youtube.com/a',
        ]
        for url in cases:
            with self.subTest(url=url), self.assertRaises(net.UnsafeURLError):
                net.public_url(url)

    def test_supported_service_boundary_cannot_be_spoofed(self):
        for url in ('https://soundcloud.com/user/song?si=a', 'https://vm.tiktok.com/id/',
                    'https://music.yandex.ru/album/1', 'https://youtu.be/abcdefghijk'):
            self.assertEqual(net.source_url(url), url)
        for url in ('https://youtube.com.evil.example/a', 'https://evil-youtube.com/',
                    'https://example.com/', 'https://youtube.com@93.184.215.14/'):
            with self.subTest(url=url), self.assertRaises(net.UnsafeURLError):
                net.source_url(url)

    def test_all_dns_answers_must_be_public(self):
        with patch.object(socket, 'getaddrinfo', return_value=dns() + dns('192.168.1.1')):
            with self.assertRaises(net.UnsafeURLError):
                net.public_addresses('cdn.example', 443)

    def test_vpn_fake_ip_uses_verified_public_dns_instead_of_allowing_reserved_ips(self):
        expected = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, ('8.8.8.8', 443))]
        with patch.object(socket, 'getaddrinfo', return_value=dns('198.18.2.57')), \
                patch.object(net, 'doh_addresses', return_value=expected) as doh:
            self.assertEqual(net.public_addresses('cdn.example', 443), expected)
        doh.assert_called_once_with('cdn.example', 443)

    def test_doh_bootstrap_uses_fixed_public_ips_without_system_dns(self):
        with patch.object(socket, 'getaddrinfo') as system:
            answers = net.public_addresses('cloudflare-dns.com', 443)
        system.assert_not_called()
        self.assertEqual([row[3][0] for row in answers], ['1.1.1.1', '1.0.0.1'])

    def test_vpn_route_is_bound_to_trusted_https_service_and_public_dns(self):
        expected = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, ('8.8.8.8', 443))]
        with patch.object(socket, 'getaddrinfo', return_value=dns('198.18.2.57')), \
                patch.object(net, 'doh_addresses', return_value=expected):
            self.assertEqual(net.transport_addresses('music.youtube.com', 443, https=True)[0][3][0], '198.18.2.57')
            self.assertEqual(net.transport_addresses('attacker.example', 443, https=True), expected)
            self.assertEqual(net.transport_addresses('music.youtube.com.evil.example', 443, https=True), expected)
            self.assertEqual(net.transport_addresses('music.youtube.com', 443, https=False), expected)
            self.assertEqual(net.transport_addresses('music.youtube.com', 80, https=True), expected)
        with patch.object(socket, 'getaddrinfo', return_value=dns('198.18.2.57')), \
                patch.object(net, 'doh_addresses', side_effect=net.UnsafeURLError('private DNS answer')):
            with self.assertRaises(net.UnsafeURLError):
                net.transport_addresses('music.youtube.com', 443, https=True)

    def test_trusted_service_never_allows_other_private_dns_ranges(self):
        for ip in ('127.0.0.1','192.168.1.1','169.254.169.254'):
            with self.subTest(ip=ip), patch.object(socket, 'getaddrinfo', return_value=dns(ip)):
                with self.assertRaises(net.UnsafeURLError):
                    net.transport_addresses('music.youtube.com', 443, https=True)

    def test_doh_response_cannot_supply_a_local_ip(self):
        net._doh_cache.clear()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.raw.read.return_value = json.dumps({'Status':0,'Answer':[{'type':1,'TTL':60,'data':'127.0.0.1'}]}).encode()
        with patch.object(net.PublicSession, 'get', return_value=response), self.assertRaises(net.UnsafeURLError):
            net.doh_addresses('untrusted.example', 443)

    def test_alternative_numeric_local_ip_encodings_are_blocked_after_resolution(self):
        for host in ('2130706433', '0177.0.0.1', '0x7f000001', '127.1'):
            with self.subTest(host=host), patch.object(socket, 'getaddrinfo', return_value=dns('127.0.0.1')):
                with self.assertRaises(net.UnsafeURLError):
                    net.public_addresses(host, 80)

    def test_connect_pins_checked_address_without_a_second_dns_lookup(self):
        sock = Mock()
        connection = net.PublicHTTPSConnection('cdn.example', timeout=10)
        with patch.object(socket, 'getaddrinfo', side_effect=[dns(), dns('127.0.0.1')]) as resolve, \
                patch.object(socket, 'socket', return_value=sock):
            self.assertIs(connection._new_conn(), sock)
        resolve.assert_called_once()
        sock.connect.assert_called_once_with(('93.184.215.14', 443))

    def test_private_dns_is_rejected_before_a_socket_is_created(self):
        with patch.object(socket, 'getaddrinfo', return_value=dns('127.0.0.1')), \
                patch.object(socket, 'socket') as create:
            with self.assertRaises(net.UnsafeURLError):
                net.PublicHTTPConnection('cdn.example', 80)._new_conn()
        create.assert_not_called()

    def test_requests_checks_redirect_before_sending_to_local_address(self):
        response = requests.Response()
        response.status_code = 302
        response.headers['Location'] = 'http://127.0.0.1/private'
        response._content = b''
        response.url = 'https://cdn.example/file'
        with net.PublicSession() as session:
            adapter = session.get_adapter(response.url)
            with patch.object(adapter, 'send', return_value=response) as send:
                with self.assertRaises(net.UnsafeURLError):
                    session.get(response.url)
            self.assertEqual(send.call_count, 1)
            self.assertIs(adapter.poolmanager.pool_classes_by_scheme['https'], net.PublicHTTPSPool)
            self.assertFalse(session.trust_env)

    def test_requests_refuses_explicit_proxy(self):
        with net.PublicSession() as session, self.assertRaises(net.UnsafeURLError):
            session.get('https://cdn.example/file', proxies={'https': 'http://127.0.0.1:8080'})

    def test_curl_refuses_private_redirect_without_second_transfer(self):
        response = SimpleNamespace(status_code=302, headers={'Location': 'http://192.168.1.1/'}, close=Mock())
        with net.PublicCurlSession() as session, \
                patch.object(socket, 'getaddrinfo', return_value=dns()), \
                patch.object(CurlSession, 'request', return_value=response) as send:
            with self.assertRaises(net.UnsafeURLError):
                session.get('https://cdn.example/file', stream=True)
            send.assert_called_once()
            self.assertFalse(send.call_args.kwargs['allow_redirects'])
            self.assertEqual(session.curl_options[CurlOpt.RESOLVE], ['cdn.example:443:93.184.215.14'])
            self.assertEqual(session.curl_options[CurlOpt.FRESH_CONNECT], 1)
        response.close.assert_called_once()

    def test_curl_redirect_rechecks_dns_and_strips_cross_origin_credentials(self):
        responses = [SimpleNamespace(status_code=302, headers={'Location': 'https://other.example/audio'}, close=Mock()),
                     SimpleNamespace(status_code=200, headers={})]
        with net.PublicCurlSession() as session, \
                patch.object(socket, 'getaddrinfo', side_effect=[dns(), dns('8.8.8.8')]) as resolve, \
                patch.object(CurlSession, 'request', side_effect=responses) as send:
            result = session.get('https://cdn.example/file', headers={'Authorization': 'Bearer fake', 'Cookie': 'private=fake', 'Accept': '*/*'})
            self.assertIs(result, responses[1])
            self.assertEqual(resolve.call_count, 2)
            self.assertEqual(send.call_args.kwargs['headers'], {'Accept': '*/*'})
            self.assertEqual(session.curl_options[CurlOpt.RESOLVE], ['other.example:443:8.8.8.8'])

    def test_curl_rejects_dns_rebinding_on_a_public_hostname_redirect(self):
        response = SimpleNamespace(status_code=302, headers={'Location': '/again'}, close=Mock())
        with net.PublicCurlSession() as session, \
                patch.object(socket, 'getaddrinfo', side_effect=[dns(), dns('10.1.2.3')]), \
                patch.object(CurlSession, 'request', return_value=response) as send:
            with self.assertRaises(net.UnsafeURLError):
                session.get('https://cdn.example/file')
            self.assertEqual(send.call_count, 1)

    def test_ydlp_uses_only_guarded_handlers_and_local_ffmpeg(self):
        with net.YoutubeDL({'quiet': True, 'proxy': 'http://127.0.0.1:9999',
                            'enable_file_urls': True, 'nocheckcertificate': True}) as ydl:
            self.assertEqual(set(ydl._request_director.handlers), {'Requests', 'CurlCFFI'})
            self.assertIsInstance(ydl._request_director.handlers['Requests'], net.PublicRequestsRH)
            self.assertIsInstance(ydl._request_director.handlers['CurlCFFI'], net.PublicCurlRH)
            self.assertEqual(ydl.params['proxy'], '')
            self.assertFalse(ydl.params['enable_file_urls'])
            self.assertFalse(ydl.params['nocheckcertificate'])
            self.assertEqual(ydl.params['postprocessor_args']['ffmpeg_i'], net.LOCAL_MEDIA_ARGS)
            with self.assertRaises(DownloadError):
                ydl.dl('unused.mp4', {'url': 'https://cdn.example/live', 'is_live': True})


class RedactionTests(unittest.TestCase):
    def test_logging_traceback_is_redacted_before_any_handler_sees_it(self):
        target = io.StringIO()
        value = 'logging-private-' + 'secret-value'
        secrets.register_secret(value)
        original = logging.getLogRecordFactory()
        try:
            with patch.object(sys, 'stdout', io.StringIO()), patch.object(sys, 'stderr', io.StringIO()):
                secrets.install_diagnostic_redaction()
                logger = logging.Logger('dl06-security-test')
                logger.addHandler(logging.StreamHandler(target))
                try:
                    raise RuntimeError(value)
                except RuntimeError:
                    logger.exception('Failed with api_key=%s', value)
        finally:
            logging.setLogRecordFactory(original)
        self.assertNotIn(value, target.getvalue())
        self.assertIn('RuntimeError', target.getvalue())

    def test_errors_hide_known_credentials_and_keep_actionable_reason(self):
        key = 'test-private-' + 'secret-value-12345'
        secrets.register_secret(key)
        text = secrets.redact(f'\x1b[31mERROR: HTTP Error 403; token={key}\x1b[0m')
        self.assertNotIn(key, text)
        self.assertNotIn('\x1b', text)
        self.assertIn('HTTP Error 403', text)

    def test_telegram_token_and_signed_url_are_hidden_without_registration(self):
        token = '123456789:' + 'A' * 35
        url = 'https://' + 'a' * 32 + '.r2.cloudflarestorage.com/private?X-Amz-Signature=fake'
        text = secrets.redact(f'https://api.telegram.org/bot{token}/sendAudio failed {url}')
        self.assertNotIn(token, text)
        self.assertNotIn('X-Amz', text)
        self.assertNotIn('/private', text)
        self.assertIn('sendAudio', text)

    def test_headers_query_parameters_and_credentials_in_urls_are_hidden(self):
        text = secrets.redact('Authorization: Bearer fake-header-value\n'
                              "{'Cookie': 'session=private-cookie-value'}\n"
                              'https://user:private-password@cdn.example/a?token=private-query-value')
        for value in ('fake-header-value', 'private-cookie-value', 'private-password', 'private-query-value'):
            self.assertNotIn(value, text)
        self.assertIn('cdn.example/a', text)

    def test_fragmented_prints_are_redacted_as_one_line(self):
        target = io.StringIO()
        stream = secrets.RedactingStream(target)
        value = 'registered-' + 'stream-secret'
        secrets.register_secret(value)
        stream.write(value[:12])
        stream.write(value[12:] + '\n')
        self.assertNotIn(value, target.getvalue())
        self.assertIn('скрыт', target.getvalue())

    def test_local_credentials_are_loaded_without_executing_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            value = 'sample-r2-' + 'private-secret'
            (root / 'r2_config.json').write_text(json.dumps({'secret_access_key': value}))
            cookie = 'sample-cookie-' + 'private-value'
            (root / 'cookies.txt').write_text('#HttpOnly_.example.com\tTRUE\t/\tTRUE\t2147483647\tsession\t' + cookie + '\n')
            secrets.load_local_secrets(root, SimpleNamespace(TOKEN='123:test'))
            text = secrets.redact(value + ' ' + cookie)
            self.assertNotIn(value, text)
            self.assertNotIn(cookie, text)


class PublicationTests(unittest.TestCase):
    def test_gitignore_excludes_private_files_and_keeps_examples(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '.gitignore').write_bytes((ROOT / '.gitignore').read_bytes())
            subprocess.run(['git', 'init', '-q', str(root)], check=True, capture_output=True)
            names = ['cookies.txt', 'whitelist.txt', 'config.py', 'Token', 'site_cookies/youtube.txt',
                     'r2_config.json', 'cloudflare_analytics.json', '_state/r2.sqlite3', 'logs/error.log',
                     '.idea/workspace.xml', 'old/old.py', 'DL-06 — копия.py']
            result = subprocess.run(['git', '-C', str(root), 'check-ignore', '--stdin'],
                                    input='\n'.join(names), text=True, encoding='utf-8', capture_output=True)
            # Git quotes non-ASCII paths; exit status is sufficient per individual path.
            self.assertEqual(result.returncode, 0)
            for name in names:
                result = subprocess.run(['git', '-C', str(root), 'check-ignore', '-q', name], capture_output=True)
                self.assertEqual(result.returncode, 0, name)
            for name in ('config.example.py', 'r2_config.example.json', 'DL-06.py'):
                self.assertEqual(subprocess.run(['git', '-C', str(root), 'check-ignore', '-q', name], capture_output=True).returncode, 1)

    def test_publication_blocks_a_staged_secret_even_after_working_copy_is_cleaned(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            value = 'private-' + 'publication-secret-value'
            (root / 'config.py').write_text('TOKEN = ' + repr(value), encoding='utf-8')
            (root / 'README.md').write_text(value, encoding='utf-8')
            subprocess.run(['git', 'init', '-q', str(root)], check=True, capture_output=True)
            subprocess.run(['git', '-C', str(root), 'add', 'README.md', 'config.py'], check=True, capture_output=True)
            (root / 'README.md').write_text('clean working copy', encoding='utf-8')
            _, findings = publication.check(root)
            self.assertTrue(any(name == 'config.py' for name, _, _ in findings))
            self.assertTrue(any(category.startswith('Git index:') for _, _, category in findings))
            self.assertNotIn(value, repr(findings))

    def test_source_manifest_excludes_private_settings_and_old_copies(self):
        names = {p.name for p in publication.publication_files(ROOT)}
        self.assertNotIn('config.py', names)
        self.assertNotIn('cookies.txt', names)
        self.assertNotIn('DL-06 — копия.py', names)
        self.assertIn('config.example.py', names)


class AccessSafetyTests(unittest.TestCase):
    def test_rate_gate_is_bounded_and_recovers_after_period(self):
        now = [0]
        gate = RateGate(2, 15, max_keys=3, clock=lambda: now[0])
        self.assertTrue(gate.allow('user'))
        self.assertTrue(gate.allow('user'))
        self.assertFalse(gate.allow('user'))
        now[0] = 16
        self.assertTrue(gate.allow('user'))
        for key in range(10):
            gate.allow(key)
        self.assertLessEqual(len(gate.events), 3)

    def test_user_id_rejects_groups_unicode_and_malformed_callbacks(self):
        self.assertEqual(telegram_user_id('000123'), '123')
        for value in ('-123', '0', '123:456', '١٢٣', '1\n', '', str(2 ** 52)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                telegram_user_id(value)


class LocalProcessingSafetyTests(unittest.TestCase):
    def test_real_ffprobe_rejects_a_local_playlist(self):
        ffprobe = ROOT / 'ffmpeg-master-latest-win64-gpl-shared/bin/ffprobe.exe'
        if not ffprobe.is_file():
            self.skipTest('FFprobe not installed')
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'playlist.m3u8'
            other = Path(folder) / 'private.ts'
            other.write_bytes(b'private content that must not be opened through a playlist')
            source.write_text('#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n'
                              '#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:1,\n' + other.as_uri() + '\n#EXT-X-ENDLIST\n')
            result = subprocess.run([str(ffprobe), '-v', 'error', *net.LOCAL_MEDIA_ARGS,
                                     '-show_format', str(source)], capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'not on whitelist', result.stderr)


class BotSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bot = telebot.TeleBot('123:test', threaded=False)
        config = SimpleNamespace(TOKEN='123:test', ADMIN_ID=1, FFMPEG_PATH='ffmpeg')
        spec = importlib.util.spec_from_file_location('dl06_security_test', ROOT / 'DL-06.py')
        cls.app = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'config': config}), patch('telebot.TeleBot', return_value=cls.bot):
            spec.loader.exec_module(cls.app)

    def setUp(self):
        self.app.ALLOWED_USER_IDS = {'1', '2'}
        self.app.LINK_RATE = RateGate(3, 15)
        self.app.DENIED_USER_RATE = RateGate(1, 900)
        self.app.DENIED_GLOBAL_RATE = RateGate(30, 3600)
        self.app.METADATA_SLOTS = __import__('threading').BoundedSemaphore(1)
        for name in ('reply_to', 'send_message', 'edit_message_text', 'answer_callback_query'):
            patcher = patch.object(self.bot, name, return_value=SimpleNamespace(message_id=123))
            patcher.start()
            self.addCleanup(patcher.stop)

    def message(self, user=2, text='https://soundcloud.com/user/track', chat_type='private'):
        return SimpleNamespace(from_user=SimpleNamespace(id=user, username=None, first_name='<b>fake admin</b>'),
                               chat=SimpleNamespace(id=user, type=chat_type), text=text)

    def test_nonadmin_cannot_use_add_user_callback_and_denial_uses_callback_api(self):
        call = SimpleNamespace(id='c', from_user=SimpleNamespace(id=2),
                               message=self.message(), data='add_id:999')
        with patch.object(self.app, 'save_whitelist') as save:
            self.app.callback_add_id(call)
        save.assert_not_called()
        self.bot.answer_callback_query.assert_called_once()
        self.bot.reply_to.assert_not_called()

    def test_invalid_admin_id_prevents_startup(self):
        config = SimpleNamespace(TOKEN='123:test', ADMIN_ID=0, FFMPEG_PATH='ffmpeg')
        spec = importlib.util.spec_from_file_location('dl06_invalid_admin', ROOT / 'DL-06.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'config': config}), patch('telebot.TeleBot') as create:
            with self.assertRaises(RuntimeError):
                spec.loader.exec_module(module)
        create.assert_not_called()

    def test_admin_callback_in_group_and_invalid_id_cannot_add_users(self):
        for chat_type, data in [('group', 'add_id:999'), ('private', 'add_id:-123'), ('private', 'add_id:1:2')]:
            call = SimpleNamespace(id='c', from_user=SimpleNamespace(id=1),
                                   message=self.message(1, chat_type=chat_type), data=data)
            with patch.object(self.app, 'save_whitelist') as save:
                self.app.callback_add_id(call)
            save.assert_not_called()
        self.assertEqual(self.app.ALLOWED_USER_IDS, {'1', '2'})

    def test_unauthorized_notifications_are_throttled_and_names_escaped(self):
        for _ in range(5):
            self.app.notify_admin_unauthorized(self.message(user=999))
        self.bot.send_message.assert_called_once()
        self.assertIn('&lt;b&gt;', self.bot.send_message.call_args.args[1])
        self.assertEqual(self.bot.send_message.call_args.kwargs['parse_mode'], 'HTML')
        self.bot.reply_to.assert_called_once()

    def test_untrusted_source_never_enters_download_or_metadata_queue(self):
        with patch.object(self.app, 'enqueue_download') as enqueue, \
                patch.object(self.app.METADATA_EXECUTOR, 'submit') as submit:
            self.app.handle_link_in_mode(self.message(text='http://127.0.0.1/private'))
        enqueue.assert_not_called()
        submit.assert_not_called()

    def test_full_metadata_queue_has_no_pending_choice_or_new_job(self):
        self.app.METADATA_SLOTS.acquire()
        with patch.object(self.app.CHOICES, 'create') as create, \
                patch.object(self.app.METADATA_EXECUTOR, 'submit') as submit:
            self.app.handle_link_in_mode(self.message())
        create.assert_not_called()
        submit.assert_not_called()

    def test_failed_submission_releases_metadata_slot_and_choice(self):
        with patch.object(self.app.METADATA_EXECUTOR, 'submit', side_effect=RuntimeError('closed')), \
                patch.object(self.app.CHOICES, 'discard') as discard:
            with self.assertRaises(RuntimeError):
                self.app.handle_link_in_mode(self.message())
        discard.assert_called_once()
        self.assertTrue(self.app.METADATA_SLOTS.acquire(blocking=False))
