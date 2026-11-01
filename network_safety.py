"""Public services over HTTP(S), including redirects and pinned destinations.

These transports are local to this application; third-party packages are not patched.
Proxies are deliberately disabled: remote DNS/proxy routing would bypass IP checks.
"""
import ipaddress
import json
import re
import socket
import subprocess
import threading
import time
from collections import OrderedDict
from pathlib import Path
from types import MethodType
from urllib.parse import urljoin, urlsplit

import requests
import urllib3
from curl_cffi import CurlOpt
from curl_cffi.requests import Session as CurlSession
from yt_dlp import YoutubeDL as BaseYoutubeDL
from yt_dlp.networking._curlcffi import CurlCFFIRH
from yt_dlp.networking._requests import RequestsRH
from yt_dlp.networking.exceptions import RequestError
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor
from yt_dlp.utils import DownloadError, PostProcessingError, determine_protocol


class UnsafeURLError(requests.exceptions.RequestException):
    pass


SOURCE_DOMAINS = (
    'youtube.com', 'youtu.be', 'youtube-nocookie.com',
    'soundcloud.com', 'on.soundcloud.com', 'snd.sc',
    'open.spotify.com', 'spotify.link', 'spoti.fi',
    'music.yandex.ru', 'music.yandex.com', 'music.yandex.by',
    'music.yandex.kz', 'music.yandex.ua', 'music.yandex.uz', 'music.yandex.com.tr',
    'tiktok.com', 'instagram.com', 'instagr.am',
)
# The owner's VPN uses Fake-IP as an outbound gateway for these provider-owned
# HTTPS domains. Never apply this to arbitrary redirected hosts or literal IPs.
VPN_HTTPS_DOMAINS = (*SOURCE_DOMAINS, 'googlevideo.com', 'ytimg.com',
                     'youtubei.googleapis.com', 'sndcdn.com', 'scdn.co',
                     'spotifycdn.com', 'tiktokcdn.com', 'tiktokcdn-us.com',
                     'tiktokcdn-eu.com', 'tiktokv.com', 'ibytedtos.com',
                     'byteoversea.com', 'cdninstagram.com', 'fbcdn.net', 'yandex.net')

# No playlist/concat demuxers: a downloaded file cannot tell FFmpeg to open a
# different local path. Native HLS/DASH downloading already produces real media.
LOCAL_MEDIA_ARGS = [
    '-protocol_whitelist', 'file,pipe',
    '-format_whitelist',
    'mov,mp3,flac,ogg,matroska,webm,aac,mpegts,mpeg,mpegvideo,wav,aiff,ape,wv,tta,'
    'ac3,eac3,amr,au,asf,flv,mjpeg,image2,image2pipe,jpeg_pipe,png_pipe,webp_pipe,'
    'bmp_pipe,tiff_pipe,gif,apng',
]
_fake_ip_network = ipaddress.ip_network('198.18.0.0/15')
_doh_cache = OrderedDict()
_doh_lock = threading.RLock()


def doh_addresses(host, port):
    """Recover real IPs from a local VPN's Fake-IP DNS, without trusting the fake IP.

    The DoH connection itself is bootstrapped to fixed public Cloudflare IPs and
    still verifies the cloudflare-dns.com TLS certificate. It never uses a proxy.
    """
    now = time.monotonic()
    with _doh_lock:
        cached = _doh_cache.pop(host, None)
        if cached and cached[0] > now:
            _doh_cache[host] = cached
            return [(socket.AF_INET6 if ':' in ip else socket.AF_INET,
                     socket.SOCK_STREAM, socket.IPPROTO_TCP,
                     (ip, port, 0, 0) if ':' in ip else (ip, port)) for ip in cached[1]]
    ips, ttls = [], []
    with PublicSession() as session:
        for record_type in ('A', 'AAAA'):
            try:
                with session.get('https://cloudflare-dns.com/dns-query',
                                 params={'name': host, 'type': record_type},
                                 headers={'Accept': 'application/dns-json'},
                                 timeout=(5, 10), stream=True) as response:
                    response.raise_for_status()
                    body = response.raw.read(65537, decode_content=True)
                    if len(body) > 65536:
                        raise ValueError()
                    data = json.loads(body)
                if data.get('Status') != 0:
                    raise ValueError()
                for answer in data.get('Answer', []):
                    if answer.get('type') not in (1, 28):
                        continue
                    ips.append(require_public_ip(answer['data']))
                    ttls.append(int(answer.get('TTL', 1)))
                if ips:
                    break
            except UnsafeURLError:
                raise
            except Exception:
                raise UnsafeURLError('Не удалось получить публичный адрес через защищённый DNS.') from None
    if not ips:
        raise UnsafeURLError('Защищённый DNS не вернул публичный адрес сервиса.')
    ips = list(dict.fromkeys(ips))
    with _doh_lock:
        _doh_cache[host] = (now + max(1, min(60, min(ttls))), ips)
        while len(_doh_cache) > 512:
            _doh_cache.popitem(last=False)
    return [(socket.AF_INET6 if ':' in ip else socket.AF_INET,
             socket.SOCK_STREAM, socket.IPPROTO_TCP,
             (ip, port, 0, 0) if ':' in ip else (ip, port)) for ip in ips]


def public_url(url):
    """Parse before a library normalizes away controls or ambiguous separators."""
    if (not isinstance(url, str) or len(url) > 8192 or '\\' in url
            or re.search(r'[\s\x00-\x1f\x7f]', url)):
        raise UnsafeURLError('Некорректная ссылка.')
    try:
        parsed = urlsplit(url)
        if (parsed.scheme.lower() not in ('http', 'https') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 80, 443)):
            raise ValueError()
        host = parsed.hostname.encode('idna').decode('ascii').lower().rstrip('.')
        if (not host or '%' in host or any(host == s or host.endswith('.' + s)
                for s in ('localhost', 'local', 'internal', 'lan', 'home', 'test', 'invalid'))):
            raise ValueError()
        # Literal IPs are also checked here, before DNS or an HTTP client sees them.
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r'[a-z0-9.-]+', host) or '..' in host:
                raise ValueError()
        else:
            require_public_ip(ip)
        return host, parsed.port or (443 if parsed.scheme.lower() == 'https' else 80)
    except (ValueError, UnicodeError):
        raise UnsafeURLError('Разрешены только публичные HTTP/HTTPS-ссылки без логина и пароля.') from None


def source_url(url):
    host, _ = public_url(url)
    if not any(host == domain or host.endswith('.' + domain) for domain in SOURCE_DOMAINS):
        raise UnsafeURLError('Поддерживаются ссылки YouTube, SoundCloud, Spotify, Яндекс.Музыки, TikTok и Instagram.')
    return url


def require_public_ip(address):
    ip = ipaddress.ip_address(address)
    if (not ip.is_global or ip.is_multicast or ip.is_unspecified
            or isinstance(ip, ipaddress.IPv6Address) and (
                ip.ipv4_mapped is not None or ip.sixtofour is not None
                or ip.teredo is not None or ip in ipaddress.ip_network('64:ff9b::/96')
                or ip in ipaddress.ip_network('64:ff9b:1::/48'))):
        raise UnsafeURLError('Доступ к локальным и служебным сетевым адресам запрещён.')
    return str(ip)


def public_addresses(host, port):
    return _destination_addresses(host, port, vpn_https=False)


def transport_addresses(host, port, *, https):
    return _destination_addresses(host, port, vpn_https=https)


def _destination_addresses(host, port, *, vpn_https):
    if host.lower().rstrip('.') == 'cloudflare-dns.com':
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, (ip, port))
                for ip in ('1.1.1.1', '1.0.0.1')]
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise UnsafeURLError('Не удалось определить публичный адрес сервиса.') from None
    if not answers:
        raise UnsafeURLError('Сервис не вернул публичный сетевой адрес.')
    # Fake-IP VPNs intentionally return this benchmarking range for public sites.
    # Validate real public DNS first; only named HTTPS providers may use the
    # owner's existing VPN gateway. Other destinations use the real public IP.
    if all(ipaddress.ip_address(address[0]) in _fake_ip_network
           for _, _, _, _, address in answers):
        public = doh_addresses(host, port)
        if (vpn_https and port == 443 and any(host == domain or host.endswith('.' + domain)
                                             for domain in VPN_HTTPS_DOMAINS)):
            # Preserve the existing VPN route only after public DNS validation,
            # only for provider-owned HTTPS hosts, with normal hostname/TLS checks.
            return list(dict.fromkeys((family, kind, protocol, address)
                                      for family, kind, protocol, _, address in answers))
        return public
    # Reject mixed public/private DNS answers, rather than picking the public one.
    for _, _, _, _, address in answers:
        require_public_ip(address[0])
    return list(dict.fromkeys((family, kind, protocol, address)
                             for family, kind, protocol, _, address in answers))


class PublicConnectionMixin:
    def _new_conn(self):
        # Resolve once, then connect to that numeric sockaddr. No second DNS lookup.
        public_url(f'{"https" if self.port == 443 else "http"}://'
                   f'{"[" + self.host + "]" if ":" in self.host else self.host}:{self.port}/')
        last_error = None
        addresses = transport_addresses(self.host, self.port,
                                        https=isinstance(self, urllib3.connection.HTTPSConnection))
        for family, kind, protocol, address in addresses:
            sock = socket.socket(family, kind, protocol)
            try:
                if isinstance(self.timeout, (int, float)) or self.timeout is None:
                    sock.settimeout(self.timeout)
                for option in self.socket_options or ():
                    sock.setsockopt(*option)
                if self.source_address:
                    sock.bind(self.source_address)
                sock.connect(address)
                return sock
            except OSError as error:
                last_error = error
                sock.close()
        raise urllib3.exceptions.NewConnectionError(self, 'Не удалось соединиться с публичным сервисом.') from last_error


class PublicHTTPConnection(PublicConnectionMixin, urllib3.connection.HTTPConnection):
    pass


class PublicHTTPSConnection(PublicConnectionMixin, urllib3.connection.HTTPSConnection):
    pass


class PublicHTTPPool(urllib3.HTTPConnectionPool):
    ConnectionCls = PublicHTTPConnection


class PublicHTTPSPool(urllib3.HTTPSConnectionPool):
    ConnectionCls = PublicHTTPSConnection


def guard_requests_session(session):
    session.trust_env = False
    session.proxies.clear()
    session.max_redirects = 5
    for adapter in session.adapters.values():
        adapter.poolmanager.pool_classes_by_scheme = {
            'http': PublicHTTPPool, 'https': PublicHTTPSPool,
        }
    original_send = session.send

    def send(self, request, **kwargs):
        public_url(request.url)
        if kwargs.get('verify') is False:
            raise UnsafeURLError('Отключение проверки сертификата запрещено.')
        if any(value for value in (kwargs.get('proxies') or {}).values()):
            raise UnsafeURLError('Прокси для загрузок отключены настройками безопасности.')
        kwargs['proxies'] = {}
        return original_send(request, **kwargs)

    # requests follows each redirect through self.send, including a prepared request.
    session.send = MethodType(send, session)
    return session


class PublicSession(requests.Session):
    def __init__(self):
        super().__init__()
        guard_requests_session(self)

    def request(self, method, url, **kwargs):
        public_url(url)
        kwargs.setdefault('timeout', (10, 30))
        return super().request(method, url, **kwargs)


class PublicCurlSession(CurlSession):
    def __init__(self, **kwargs):
        super().__init__(trust_env=False, **kwargs)
        self._guard_lock = threading.RLock()

    def request(self, method, url, **kwargs):
        # curl's native redirect/DNS handling cannot be checked by Python sockets.
        # Follow redirects here and supply only checked IPs with CURLOPT_RESOLVE.
        with self._guard_lock:
            if kwargs.get('proxy') or any((kwargs.get('proxies') or {}).values()) or self.proxies:
                raise UnsafeURLError('Прокси для загрузок отключены настройками безопасности.')
            if kwargs.get('verify') is False or self.verify is False:
                raise UnsafeURLError('Отключение проверки сертификата запрещено.')
            limit = min(int(kwargs.pop('max_redirects', 5)), 5)
            follow = kwargs.pop('allow_redirects', True)
            headers = dict(kwargs.pop('headers', None) or {})
            for hop in range(limit + 1):
                host, port = public_url(url)
                addresses = transport_addresses(host, port, https=urlsplit(url).scheme.lower() == 'https')
                ips = list(dict.fromkeys(address[0] for _, _, _, address in addresses))
                formatted = ','.join(f'[{ip}]' if ':' in ip else ip for ip in ips)
                self.curl_options = {
                    CurlOpt.RESOLVE: [f'{host}:{port}:{formatted}'],
                    CurlOpt.PROXY: '', CurlOpt.FRESH_CONNECT: 1,
                    CurlOpt.FORBID_REUSE: 1, CurlOpt.DNS_CACHE_TIMEOUT: 0,
                    CurlOpt.PROTOCOLS_STR: 'http,https',
                    CurlOpt.REDIR_PROTOCOLS_STR: 'http,https',
                }
                response = super().request(method, url, headers=headers,
                                           allow_redirects=False, **kwargs)
                location = response.headers.get('Location')
                if not follow or response.status_code not in (301, 302, 303, 307, 308) or not location:
                    return response
                response.close()
                if hop == limit:
                    raise UnsafeURLError('Слишком много перенаправлений у ссылки.')
                target = urljoin(url, location)
                # Validate before the next call, even if the response has no body.
                public_url(target)
                if (urlsplit(url).scheme, urlsplit(url).netloc) != (urlsplit(target).scheme, urlsplit(target).netloc):
                    headers = {key: value for key, value in headers.items()
                               if key.lower() not in ('authorization', 'cookie', 'host', 'proxy-authorization')}
                    for key in ('auth', 'cookies'):
                        kwargs.pop(key, None)
                if response.status_code == 303 and method.upper() != 'HEAD' or response.status_code in (301, 302) and method.upper() == 'POST':
                    method = 'GET'
                    for key in ('data', 'content', 'json', 'files'):
                        kwargs.pop(key, None)
                    headers = {key: value for key, value in headers.items()
                               if key.lower() not in ('content-type', 'content-length', 'transfer-encoding')}
                elif any(hasattr(kwargs.get(key), 'read') for key in ('data', 'content')):
                    raise UnsafeURLError('Повторная отправка потока после редиректа запрещена.')
                kwargs.pop('params', None)
                url = target


class PublicRequestsRH(RequestsRH):
    RH_KEY = 'Requests'

    def _create_instance(self, *args, **kwargs):
        return guard_requests_session(super()._create_instance(*args, **kwargs))


class PublicCurlRH(CurlCFFIRH):
    RH_KEY = 'CurlCFFI'

    def _create_instance(self, cookiejar=None):
        return PublicCurlSession(cookies=cookiejar)

    def _send(self, request):
        try:
            public_url(request.url)
            return super()._send(request)
        except UnsafeURLError as error:
            raise RequestError(str(error)) from None


def _local_metadata(pp, path, opts=()):
    # yt-dlp's ordinary ffprobe helpers do not use postprocessor_args. Replace
    # those helpers on our PP instances too, so probing cannot bypass the policy.
    text = str(path)
    if text.startswith(('\\\\', '//')) or re.match(r'^[a-zA-Z][\w+.-]*://', text):
        raise PostProcessingError('Анализировать можно только локальный медиафайл запроса.')
    source = Path(path).resolve()
    root = Path(pp._downloader.params.get('paths', {}).get('home') or source.parent).resolve()
    if not source.is_relative_to(root) or not source.is_file() or not pp.probe_available:
        raise PostProcessingError('Не удалось безопасно открыть медиафайл для анализа.')
    try:
        result = subprocess.run([
            pp.probe_executable, '-v', 'error', '-show_format', '-show_streams',
            '-of', 'json', *opts, *LOCAL_MEDIA_ARGS, str(source),
        ], capture_output=True, timeout=60)
        if result.returncode:
            raise ValueError()
        return json.loads(result.stdout)
    except Exception:
        raise PostProcessingError('Не удалось определить формат локального медиафайла.') from None


def _local_audio_codec(pp, path):
    return next((stream.get('codec_name') for stream in _local_metadata(pp, path).get('streams', [])
                 if stream.get('codec_type') == 'audio'), None)


class YoutubeDL(BaseYoutubeDL):
    def __init__(self, params=None, *args, **kwargs):
        params = dict(params or {})
        params.update({
            'proxy': '', 'enable_file_urls': False, 'nocheckcertificate': False,
            'hls_prefer_native': True, 'external_downloader': 'native',
            # Even an internal HLS fallback must never give FFmpeg network access.
            'external_downloader_args': {'ffmpeg_i': list(LOCAL_MEDIA_ARGS)},
            'postprocessor_args': {'ffmpeg_i': list(LOCAL_MEDIA_ARGS)},
        })
        super().__init__(params, *args, **kwargs)

    def build_request_director(self, handlers, preferences=None):
        # No unguarded urllib, websocket or plugin transport as fallback.
        return super().build_request_director((PublicRequestsRH, PublicCurlRH), preferences)

    def run_pp(self, pp, infodict):
        if isinstance(pp, FFmpegPostProcessor):
            pp.get_metadata_object = MethodType(_local_metadata, pp)
            pp.get_audio_codec = MethodType(_local_audio_codec, pp)
        return super().run_pp(pp, infodict)

    def dl(self, name, info, subtitle=False, test=False):
        protocols = determine_protocol(info).split('+')
        if (info.get('is_live') or info.get('section_start') or info.get('section_end')
                or any(protocol not in ('http', 'https', 'm3u8', 'm3u8_native',
                        'http_dash_segments', 'http_dash_segments_generator') for protocol in protocols)):
            raise DownloadError('Этот поток требует внешнего сетевого загрузчика и не поддерживается ботом.')
        for fmt in info.get('requested_formats') or [info]:
            public_url(fmt['url'])
        return super().dl(name, info, subtitle=subtitle, test=test)
