"""update checker. never actually hits github"""
import hashlib
import io
import json
import os
import unittest
import zipfile
from unittest import mock

from league_vcs import selfupdate, updates


def release(tag):
    return {'version': '.'.join(map(str, updates.parse_version(tag))), 'url': 'https://example.invalid', 'notes': '', 'published': None}


class UpdateTest(unittest.TestCase):
    def setUp(self):
        updates._cache.update(at=0, release=None)

    def test_parse_version(self):
        self.assertEqual(updates.parse_version('v1.2.3'), (1, 2, 3))
        self.assertEqual(updates.parse_version('1.10'), (1, 10, 0))
        self.assertIsNone(updates.parse_version('nightly'))

    def test_newer_release_is_reported(self):
        with mock.patch.object(updates, '__version__', '1.0.0'), \
                mock.patch.object(updates, '_fetch_latest', return_value=release('v1.1.0')):
            self.assertEqual(updates.newer_than_running()['version'], '1.1.0')

    def test_same_or_older_is_ignored(self):
        with mock.patch.object(updates, '__version__', '1.2.0'):
            for tag in ('v1.2.0', 'v1.1.9'):
                with mock.patch.object(updates, '_fetch_latest', return_value=release(tag)):
                    self.assertIsNone(updates.newer_than_running(force=True))

    def test_offline_or_private_repo_is_quiet(self):
        with mock.patch.object(updates, '_fetch_latest', return_value=None):
            self.assertIsNone(updates.newer_than_running(force=True))

    def test_release_link_has_to_be_ours(self):
        def fake_api(url):
            body = json.dumps({'tag_name': 'v2.0.0', 'html_url': url}).encode()
            return mock.MagicMock(__enter__=lambda s: mock.MagicMock(read=lambda: body))
        ours = updates.RELEASES_URL + '/tag/v2.0.0'
        for given, want in ((ours, ours), ('https://evil.example/x', updates.RELEASES_URL),
                            (updates.RELEASES_URL + '.evil.example/', updates.RELEASES_URL)):
            with mock.patch('urllib.request.urlopen', return_value=fake_api(given)):
                self.assertEqual(updates._fetch_latest()['url'], want)

    def test_result_is_cached(self):
        with mock.patch.object(updates, '_fetch_latest', return_value=release('v9.0.0')) as fetch:
            updates.latest()
            updates.latest()
            self.assertEqual(fetch.call_count, 1)
            updates.latest(force=True)
            self.assertEqual(fetch.call_count, 2)

    def test_download_links_have_to_be_ours(self):
        ours = updates.DOWNLOADS_URL + 'v2.0.0/L-Recall-v2.0.0-win64.zip'
        body = json.dumps({'tag_name': 'v2.0.0', 'html_url': '', 'assets': [
            {'browser_download_url': 'https://evil.example/L-Recall-v2.0.0-win64.zip'},
            {'browser_download_url': ours, 'size': 5},
            {'browser_download_url': ours + '.sha256'}]}).encode()
        fake = mock.MagicMock(__enter__=lambda s: mock.MagicMock(read=lambda: body))
        with mock.patch('urllib.request.urlopen', return_value=fake):
            r = updates._fetch_latest()
        self.assertEqual((r['zip'], r['sha256'], r['size']), (ours, ours + '.sha256', 5))


class SelfUpdateTest(unittest.TestCase):
    def serve(self, files):
        """urlopen that hands out these bytes by url"""
        def fake(req, timeout=None):
            data = files[req.full_url]
            m = mock.MagicMock()
            m.__enter__ = lambda s: m
            m.__exit__ = lambda *a: False
            m.headers = {'Content-Length': str(len(data))}
            chunks = iter([data, b''])
            m.read = lambda *a: next(chunks)
            return m
        return mock.patch('urllib.request.urlopen', side_effect=fake)

    def release(self, zip_bytes, sha=None):
        base = updates.DOWNLOADS_URL + 'v9.0.0/L-Recall-v9.0.0-win64.zip'
        sha = sha or hashlib.sha256(zip_bytes).hexdigest().upper() + '\r\n'
        return {'zip': base, 'sha256': base + '.sha256'}, {base: zip_bytes, base + '.sha256': sha.encode()}

    def make_zip(self, names):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            for n in names:
                z.writestr(n, b'x')
        return buf.getvalue()

    def test_good_update_unpacks(self):
        release, files = self.release(self.make_zip(['L-Recall.exe', 'lib/a.pyd', 'python3.dll']))
        with self.serve(files):
            new = selfupdate.download(release)
        self.assertTrue(os.path.isfile(os.path.join(new, 'lib', 'a.pyd')))

    def test_wrong_hash_is_refused(self):
        release, files = self.release(self.make_zip(['L-Recall.exe']), sha='0' * 64)
        with self.serve(files), self.assertRaises(ValueError):
            selfupdate.download(release)

    def test_zip_escaping_the_folder_is_refused(self):
        release, files = self.release(self.make_zip(['L-Recall.exe', '../../evil.exe']))
        with self.serve(files), self.assertRaises(ValueError):
            selfupdate.download(release)

    def test_only_our_downloads(self):
        with self.assertRaises(ValueError):
            selfupdate.download({'zip': 'https://evil.example/x.zip', 'sha256': 'https://evil.example/x.sha256'})


if __name__ == '__main__':
    unittest.main()
