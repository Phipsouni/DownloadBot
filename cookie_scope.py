"""Ограниченный доступ к cookies для поддерживаемых видеосервисов."""

import os
from urllib.parse import urlsplit

from yt_dlp import cookies as ytdlp_cookies


SITE_DOMAINS = {
    'youtube': {
        'links': ('youtube.com', 'youtu.be', 'youtube-nocookie.com'),
        'cookies': ('youtube.com',),
    },
    'instagram': {
        'links': ('instagram.com', 'instagr.am'),
        'cookies': ('instagram.com',),
    },
    'tiktok': {
        'links': ('tiktok.com',),
        'cookies': ('tiktok.com',),
    },
}


def matches_domain(host, domain):
    host = host.lower().lstrip('.').rstrip('.')
    return host == domain or host.endswith('.' + domain)


def site_for_url(url):
    host = urlsplit(url).hostname
    if not host:
        return None
    for site, domains in SITE_DOMAINS.items():
        if any(matches_domain(host, domain) for domain in domains['links']):
            return site
    return None


def cookie_file_for_url(url, cookie_files):
    site = site_for_url(url)
    if not site:
        return None
    filename = cookie_files.get(site)
    if filename and os.path.isfile(filename):
        return filename
    return None


def load_scoped_cookie_file(filename, site):
    """Берёт из отдельного файла только cookies своего сервиса, без записи обратно."""
    if site not in SITE_DOMAINS:
        raise ValueError('Cookies для этого сайта запрещены.')
    source = ytdlp_cookies.YoutubeDLCookieJar(filename)
    source.load()
    jar = ytdlp_cookies.YoutubeDLCookieJar()
    for cookie in source:
        if any(matches_domain(cookie.domain, domain)
               for domain in SITE_DOMAINS[site]['cookies']):
            jar.set_cookie(cookie)
    return jar
