"""update checker. never actually hits github"""
import json
import unittest
from unittest import mock

from league_vcs import updates


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


if __name__ == '__main__':
    unittest.main()
