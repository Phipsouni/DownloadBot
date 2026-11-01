"""Redact diagnostics; credentials themselves remain only in ignored local files."""
import html
import json
import logging
import re
import threading
import traceback
from pathlib import Path
from urllib.parse import quote, quote_plus, urlsplit, urlunsplit

_secrets = set()
_lock = threading.RLock()
_ansi = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\[(?:\d+;)*\d+m')
_token = re.compile(r'\d{5,}:[A-Za-z0-9_-]{20,}')
_url = re.compile(r'https?://[^\s<>\x00-\x1f\x7f\"\']+', re.I)
_sensitive = r'(?:authorization|proxy-authorization|cookie|set-cookie|api[_-]?(?:token|key)|access[_-]?token|refresh[_-]?token|secret[_-]?access[_-]?key|aws[_-]?secret[_-]?access[_-]?key|access[_-]?key[_-]?id|password|client[_-]?secret|x-amz-signature|x-amz-credential)'
_quoted = re.compile(r'([\"\']?' + _sensitive + r'[\"\']?\s*[:=]\s*)([\"\'])(.*?)(\2)', re.I)
_header = re.compile(r'(?im)^(\s*(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*).*$')
_assignment = re.compile(r'(\b' + _sensitive + r'\s*[=:]\s*)[^\s,;\}\]]+', re.I)


def register_secret(value):
    if isinstance(value, str) and len(value) >= 6:
        with _lock:
            _secrets.update((value, quote(value, safe=''), quote_plus(value), html.escape(value)))


def load_local_secrets(root, config):
    """Read only known local credential files, never discover browser credentials."""
    register_secret(getattr(config, 'TOKEN', None))
    root = Path(root)
    for name in ('r2_config.json', 'cloudflare_analytics.json'):
        try:
            data = json.loads((root / name).read_text(encoding='utf-8-sig'))
            for key, value in data.items():
                if re.search(r'token|secret|access_key', key, re.I):
                    register_secret(value)
        except (OSError, ValueError, AttributeError):
            pass
    files = [root / 'cookies.txt', *getattr(config, 'YTDLP_SITE_COOKIE_FILES', {}).values()]
    for file in files:
        try:
            for line in Path(file).read_text(encoding='utf-8-sig').splitlines():
                parts = line.removeprefix('#HttpOnly_').split('\t')
                if len(parts) == 7 and (not line.startswith('#') or line.startswith('#HttpOnly_')):
                    register_secret(parts[6])
        except (OSError, ValueError, TypeError):
            pass


def _safe_url(match):
    value = match.group()
    try:
        parsed = urlsplit(value)
        if '.r2.cloudflarestorage.com' in (parsed.hostname or '') or re.search(r'x-amz-|signature=', value, re.I):
            return '[закрытая ссылка скрыта]'
        host = parsed.hostname or '[адрес скрыт]'
        if ':' in host:
            host = '[' + host + ']'
        if parsed.port:
            host += ':' + str(parsed.port)
        return urlunsplit((parsed.scheme, host, parsed.path, '[параметры скрыты]' if parsed.query else '', ''))
    except ValueError:
        return '[ссылка скрыта]'


def redact(value):
    text = _ansi.sub('', str(value))
    with _lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for secret in secrets:
        text = text.replace(secret, '[секрет скрыт]')
    text = _token.sub('[токен скрыт]', text)
    text = _url.sub(_safe_url, text)
    text = _quoted.sub(lambda m: m[1] + m[2] + '[скрыто]' + m[4], text)
    text = _header.sub(lambda m: m[1] + '[скрыто]', text)
    return _assignment.sub(lambda m: m[1] + '[скрыто]', text).strip()


class RedactingStream:
    def __init__(self, stream):
        self.stream = stream
        self.pending = ''
        self.lock = threading.RLock()

    def write(self, text):
        with self.lock:
            self.pending += text
            while '\n' in self.pending:
                line, self.pending = self.pending.split('\n', 1)
                self.stream.write(redact(line) + '\n')
            # Bound memory even for a library that never emits a newline.
            if len(self.pending) > 65536:
                self.stream.write('[слишком длинная строка диагностики скрыта]\n')
                self.pending = ''
        return len(text)

    def flush(self):
        with self.lock:
            if self.pending:
                self.stream.write(redact(self.pending))
                self.pending = ''
            self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def install_diagnostic_redaction():
    import sys
    for name in ('stdout', 'stderr'):
        stream = getattr(sys, name)
        if not isinstance(stream, RedactingStream):
            setattr(sys, name, RedactingStream(stream))
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        record.msg, record.args = redact(record.getMessage()), ()
        if record.exc_info:
            record.exc_text = redact(''.join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        return record

    logging.setLogRecordFactory(factory)
