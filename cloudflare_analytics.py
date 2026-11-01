"""Read-only R2 operation analytics. The S3 key is never used as an API token."""
import hashlib
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

MOSCOW = timezone(timedelta(hours=3))


class AnalyticsError(Exception):
    pass


QUERY = '''query R2Operations($account: string!, $bucket: string!, $start: Time!, $end: Time!) {
  viewer { accounts(filter: {accountTag: $account}) {
    r2OperationsAdaptiveGroups(limit: 1000,
      filter: {bucketName: $bucket, datetime_geq: $start, datetime_lt: $end}) {
        dimensions { actionType }
        sum { requests }
    }
  } }
}'''

CLASS_A = set('ListBuckets PutBucket ListObjects ListObjectsV2 PutObject CopyObject '
              'CompleteMultipartUpload CreateMultipartUpload LifecycleStorageTierTransition '
              'ListMultipartUploads UploadPart UploadPartCopy ListParts PutBucketEncryption '
              'PutBucketCors PutBucketLifecycleConfiguration'.lower().split())
CLASS_B = set('HeadBucket HeadObject GetObject UsageSummary GetBucketEncryption '
              'GetBucketLocation GetBucketCors GetBucketLifecycleConfiguration'.lower().split())
FREE = set('DeleteObject DeleteObjects DeleteBucket AbortMultipartUpload'.lower().split())
_cache = {}
_lock = threading.Lock()


def monthly_operations(filename, settings, now=None, post=None):
    try:
        config = json.loads(Path(filename).read_text(encoding='utf-8-sig'))
        account = config['account_id']
        token = config['api_token']
        if (not re.fullmatch(r'[a-f0-9]{32}', account)
                or not isinstance(token, str) or not token.strip()
                or not settings.endpoint_url.startswith(f'https://{account}.')):
            raise ValueError()
    except FileNotFoundError:
        raise AnalyticsError('Не подключён ключ чтения аналитики Cloudflare.') from None
    except (OSError, ValueError, KeyError, TypeError):
        raise AnalyticsError('Проверьте настройки ключа аналитики Cloudflare.') from None
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(MOSCOW)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    key = (account, settings.bucket, start.isoformat(), hashlib.sha256(token.encode()).hexdigest())
    with _lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < 300:
            return dict(cached[1])
    variables = {'account': account, 'bucket': settings.bucket,
                 'start': start.astimezone(timezone.utc).isoformat(),
                 'end': now.astimezone(timezone.utc).isoformat()}
    try:
        response = (post or requests.post)(
            'https://api.cloudflare.com/client/v4/graphql',
            headers={'Authorization': f'Bearer {token}'},
            json={'query': QUERY, 'variables': variables}, timeout=(10, 30))
        response.raise_for_status()
        body = response.json()
        if body.get('errors'):
            raise ValueError()
        accounts = body['data']['viewer']['accounts']
        if len(accounts) != 1:
            raise ValueError()
        rows = accounts[0]['r2OperationsAdaptiveGroups']
        if len(rows) >= 1000:
            raise ValueError()
        result = {'total': 0, 'a': 0, 'b': 0, 'free': 0, 'other': 0,
                  'month': start.strftime('%m.%Y'), 'as_of': now.timestamp()}
        for row in rows:
            count = int(row['sum']['requests'])
            if count < 0:
                raise ValueError()
            action = re.sub('[^a-z0-9]', '', row['dimensions']['actionType'].lower())
            category = ('a' if action in CLASS_A else 'b' if action in CLASS_B else
                        'free' if action in FREE else 'other')
            result['total'] += count
            result[category] += count
    except Exception:
        # Neither the bearer token nor upstream response/exception belongs in Telegram or logs.
        raise AnalyticsError('Статистика операций недоступна: проверьте сеть и права чтения аналитики.') from None
    with _lock:
        _cache[key] = (time.monotonic(), dict(result))
    return result
