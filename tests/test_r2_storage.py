import json
import tempfile
import threading
import sqlite3
import hashlib
from contextlib import closing
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from r2_storage import (R2Settings, R2Storage, StorageError, StorageNotConfigured,
                        StorageQuotaError, attachment_header, create_client)


ENDPOINT = 'https://' + 'a' * 32 + '.r2.cloudflarestorage.com'


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.uploads = []
        self.deleted = []
        self.fail_upload = False
        self.fail_delete = False
        self.lock = threading.Lock()
        self.signing_args = None

    def head_bucket(self, **kwargs):
        return {}

    def list_objects_v2(self, **kwargs):
        with self.lock:
            return {'Contents': [{'Key': key, 'Size': size} for key, size in self.objects.items()]}

    def upload_file(self, filename, bucket, key, **kwargs):
        with self.lock:
            self.objects[key] = Path(filename).stat().st_size
            self.uploads.append((key, kwargs))
        if self.fail_upload:
            raise RuntimeError('SDK failure with test-secret and a signed URL')

    def generate_presigned_url(self, operation, **kwargs):
        self.signing_args = (operation, kwargs)
        return f'{ENDPOINT}/{kwargs["Params"]["Bucket"]}/{kwargs["Params"]["Key"]}?signed=test'

    def delete_object(self, Bucket, Key):
        if self.fail_delete:
            raise RuntimeError('offline')
        with self.lock:
            self.objects.pop(Key, None)
            self.deleted.append(Key)

    def list_multipart_uploads(self, **kwargs):
        return {'Uploads': []}

    def abort_multipart_upload(self, **kwargs):
        return {}


class R2StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.file = self.root / 'track.mp3'
        self.file.write_bytes(b'1234567')
        self.now = [1000.0]
        self.client = FakeS3()
        self.settings = R2Settings(ENDPOINT, 'dl06-downloads', 'test-access', 'test-secret',
                                   ttl_seconds=600, max_file_bytes=20, max_storage_bytes=30)
        self.store = R2Storage(self.settings, self.root / 'state.sqlite3', self.client,
                               clock=lambda: self.now[0])

    def test_upload_signs_only_one_private_object_and_does_not_set_public_acl(self):
        stored = self.store.upload(self.file, 'Исполнитель — Трек.mp3')
        self.assertTrue(stored.key.startswith('dl06/'))
        self.assertEqual(stored.expires_at, 1600)
        self.assertEqual(stored.size, 7)
        self.assertNotIn('ACL', self.client.uploads[0][1]['ExtraArgs'])
        operation, arguments = self.client.signing_args
        self.assertEqual(operation, 'get_object')
        self.assertEqual(arguments['ExpiresIn'], 600)
        self.assertEqual(arguments['Params']['Key'], stored.key)
        self.assertTrue(self.file.exists())
        self.assertNotIn(stored.url, repr(stored))

    def test_upload_callback_handles_concurrent_chunks_and_retry_rewinds(self):
        original_upload = self.client.upload_file
        def upload(filename, bucket, key, **kwargs):
            callback = kwargs['Callback']
            callback(3)
            callback(-3)
            workers = [threading.Thread(target=callback, args=(n,)) for n in (3, 4)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            original_upload(filename, bucket, key, **kwargs)
        self.client.upload_file = upload
        observed = []
        self.store.upload(self.file, progress_callback=lambda done, total: observed.append((done, total)))
        self.assertEqual(observed[:2], [(3, 7), (0, 7)])
        self.assertEqual(observed[-1], (7, 7))
        self.assertTrue(all(0 <= done <= total for done, total in observed))

    def test_actual_sdk_signer_produces_time_limited_get_without_network(self):
        client = create_client(self.settings)
        url = client.generate_presigned_url(
            'get_object', Params={'Bucket': self.settings.bucket, 'Key': 'dl06/test.mp3'},
            ExpiresIn=600)
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(urlsplit(url).hostname, urlsplit(ENDPOINT).hostname)
        self.assertEqual(query['X-Amz-Expires'], ['600'])
        self.assertIn('X-Amz-Signature', query)
        self.assertNotIn('test-secret', url)

    def test_existing_untracked_objects_count_toward_quota(self):
        self.client.objects['another-file'] = 25
        with self.assertRaises(StorageQuotaError):
            self.store.upload(self.file)
        self.assertFalse(self.client.uploads)
        self.assertEqual(self.client.objects, {'another-file': 25})

    def test_personal_quota_is_persistent_isolated_and_released_on_deletion(self):
        first = self.store.upload(self.file, user_id='2', user_limit_bytes=10)
        restarted = R2Storage(self.settings, self.store.state_file, self.client, clock=lambda:self.now[0])
        with self.assertRaises(StorageQuotaError):
            restarted.upload(self.file, user_id='2', user_limit_bytes=10)
        restarted.upload(self.file, user_id='3', user_limit_bytes=10)
        restarted.upload(self.file, user_id='1', user_limit_bytes=None)
        restarted.revoke(first)
        restarted.upload(self.file, user_id='2', user_limit_bytes=10)
        self.assertEqual(sum(self.client.objects.values()), 21)
        self.now[0] = 2000
        restarted.cleanup_expired()
        self.assertFalse(self.client.objects)
        restarted.upload(self.file, user_id='2', user_limit_bytes=10)

    def test_admin_personal_exemption_does_not_bypass_global_quota(self):
        self.client.objects['untracked'] = 25
        with self.assertRaises(StorageQuotaError):
            self.store.upload(self.file, user_id='1', user_limit_bytes=None)

    def test_concurrent_uploads_cannot_exceed_personal_quota(self):
        def upload():
            try:
                R2Storage(self.settings, self.store.state_file, self.client).upload(
                    self.file, user_id='2', user_limit_bytes=10)
                return True
            except StorageQuotaError:
                return False
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _:upload(),range(2)))
        self.assertEqual(sorted(results),[False,True])

    def test_old_six_column_database_is_migrated_without_losing_objects(self):
        path = self.root/'legacy.sqlite3'
        owner = hashlib.sha256(f'{self.settings.endpoint_url}/{self.settings.bucket}'.encode()).hexdigest()
        with closing(sqlite3.connect(path)) as db:
            db.execute('''CREATE TABLE r2_objects (owner TEXT, object_key TEXT, size INTEGER,
                       created_at REAL, expires_at REAL, status TEXT, PRIMARY KEY(owner,object_key))''')
            db.execute('INSERT INTO r2_objects VALUES (?,?,?,?,?,?)',
                       (owner,'dl06/legacy',7,900,1600,'ready'))
            db.commit()
        self.client.objects['dl06/legacy'] = 7
        migrated = R2Storage(self.settings,path,self.client,clock=lambda:1000)
        self.assertEqual(migrated.status_snapshot()['active_links'],1)
        with closing(sqlite3.connect(path)) as db:
            self.assertIsNone(db.execute('SELECT user_id FROM r2_objects').fetchone()[0])

    def test_concurrent_uploads_cannot_both_exceed_quota(self):
        settings = R2Settings(ENDPOINT, 'dl06-downloads', 'test-access', 'test-secret',
                              max_file_bytes=10, max_storage_bytes=10)

        def upload():
            store = R2Storage(settings, self.root / 'concurrent.sqlite3', self.client)
            try:
                store.upload(self.file)
                return True
            except StorageQuotaError:
                return False

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: upload(), range(2)))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(sum(self.client.objects.values()), 7)

    def test_failed_upload_removes_partial_object_and_reservation(self):
        self.client.fail_upload = True
        with self.assertRaises(StorageError) as captured:
            self.store.upload(self.file)
        self.assertNotIn('test-secret', str(captured.exception))
        self.assertFalse(self.client.objects)
        self.client.fail_upload = False
        self.store.upload(self.file)
        self.assertEqual(sum(self.client.objects.values()), 7)

    def test_cleanup_survives_restart_and_removes_only_expired_files(self):
        first = self.store.upload(self.file)
        self.now[0] += 300
        second = self.store.upload(self.file)
        self.now[0] = first.expires_at + 1
        restarted = R2Storage(self.settings, self.root / 'state.sqlite3', self.client,
                              clock=lambda: self.now[0])
        self.assertEqual(restarted.cleanup_expired(), 1)
        self.assertNotIn(first.key, self.client.objects)
        self.assertIn(second.key, self.client.objects)

    def test_failed_delete_is_retried_without_releasing_quota(self):
        self.store.upload(self.file)
        self.now[0] += 601
        self.client.fail_delete = True
        self.assertEqual(self.store.cleanup_expired(), 0)
        self.assertEqual(len(self.client.objects), 1)
        self.client.fail_delete = False
        self.assertEqual(self.store.cleanup_expired(), 1)
        self.assertFalse(self.client.objects)

    def test_revoke_removes_object_if_link_cannot_be_delivered(self):
        stored = self.store.upload(self.file)
        self.store.revoke(stored)
        self.assertFalse(self.client.objects)

    def test_configuration_rejects_non_r2_endpoints_and_hides_keys(self):
        self.assertNotIn('test-secret', repr(self.settings))
        self.assertNotIn('test-access', repr(self.settings))
        for endpoint in ('http://' + 'a'*32 + '.r2.cloudflarestorage.com',
                         'https://example.com', ENDPOINT + '/other', ENDPOINT + '?key=abc'):
            with self.subTest(endpoint=endpoint), self.assertRaises(StorageError):
                R2Settings(endpoint, 'dl06-downloads', 'test-access', 'test-secret')

    def test_missing_and_disabled_configuration_do_not_start_client(self):
        with self.assertRaises(StorageNotConfigured):
            R2Settings.from_file(self.root / 'missing.json')
        filename = self.root / 'disabled.json'
        filename.write_text(json.dumps({'enabled': False}))
        with self.assertRaises(StorageNotConfigured):
            R2Settings.from_file(filename)

    def test_attachment_header_supports_unicode_and_prevents_header_injection(self):
        header = attachment_header('Альбом\r\nInjected: header.zip')
        self.assertNotIn('\r', header)
        self.assertNotIn('\n', header)
        self.assertIn("filename*=UTF-8''", header)


if __name__ == '__main__':
    unittest.main()
