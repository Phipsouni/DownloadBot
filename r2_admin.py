"""Проверка подключения R2 без вывода ключей и подписанных ссылок."""

import argparse
import json
import tempfile
from pathlib import Path

import requests

from r2_storage import R2Settings, R2Storage, StorageError


ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('check', 'smoke', 'cleanup'))
    args = parser.parse_args()
    settings = R2Settings.from_file(ROOT / 'r2_config.json')
    store = R2Storage(settings, ROOT / '_state/r2.sqlite3')
    if args.action == 'check':
        print(json.dumps(store.check_connection(), ensure_ascii=False))
    elif args.action == 'cleanup':
        print(json.dumps({'deleted_expired_objects': store.cleanup_expired()}))
    else:
        download_root = ROOT / 'downloads'
        download_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='r2-smoke-', dir=download_root) as folder:
            if not Path(folder).resolve().is_relative_to(download_root.resolve()):
                raise StorageError('Некорректная временная папка проверки.')
            filename = Path(folder) / 'dl06-check.txt'
            payload = b'DL-06 private R2 download check\n'
            filename.write_bytes(payload)
            stored = store.upload(filename, filename.name)
            try:
                response = requests.get(stored.url, timeout=30)
                if response.status_code != 200 or response.content != payload:
                    raise StorageError('Проверка скачивания из R2 не прошла.')
                if 'attachment' not in response.headers.get('Content-Disposition', ''):
                    raise StorageError('R2 не вернул заголовок скачивания файла.')
                print(json.dumps({'upload': True, 'download': True, 'attachment_header': True}))
            finally:
                store.revoke(stored)
                print(json.dumps({'test_object_deleted': True}))


if __name__ == '__main__':
    try:
        main()
    except StorageError as error:
        print(str(error))
        raise SystemExit(1)
    except Exception:
        print('Проверка R2 не выполнена: проверьте сеть и настройки. Секреты не выводятся.')
        raise SystemExit(1)
