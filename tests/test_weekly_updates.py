import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'TelegramManager.app/Contents/Resources'))
os.environ.setdefault('TG_SESSION_TOKEN', 'unit-test-token')
import updates
import state
import server


class WeeklyUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'client_update_monitor.json'
        patch = mock.patch.object(state, 'DATA_DIR', self.temp.name)
        patch.start(); self.addCleanup(patch.stop)
        updates._MONITOR_ERROR = ''
        self.row = {'client': 'AyuGram', 'tag': 'v7.3.2', 'version': '7.3.2', 'sha256': 'a'*64,
                    'installed_version': '7.3.1', 'release_url': 'https://github.com/AyuGram/AyuGramDesktop/releases/tag/v7.3.2'}
        patch = mock.patch.object(updates, 'check_releases', side_effect=lambda: {'clients': [dict(self.row)]})
        self.check = patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(updates, '_extra_ayu_candidates', return_value=([], []))
        self.extra = patch.start(); self.addCleanup(patch.stop)

    def stamp(self, value):
        return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo('America/New_York')).timestamp()

    def run_at(self, value):
        stamp = self.stamp(value)
        with mock.patch.object(updates.time, 'time', return_value=stamp):
            return updates.run_weekly_check(stamp)

    def test_missed_startup_weekly_slot_and_dst(self):
        self.run_at('2026-03-02T11:00')
        self.run_at('2026-03-08T11:00')
        self.assertEqual(self.check.call_count, 1)
        status = self.run_at('2026-03-09T10:00')
        self.assertEqual(self.check.call_count, 2)
        self.assertEqual(status['next_check'], self.stamp('2026-03-16T10:00'))
        self.assertEqual(updates._weekly_slot(self.stamp('2026-03-02T11:00'))[1] - self.stamp('2026-03-02T10:00'), 167*3600)
        self.assertEqual(updates._weekly_slot(self.stamp('2026-10-26T11:00'))[1] - self.stamp('2026-10-26T10:00'), 169*3600)
        self.run_at('2026-03-01T11:00')
        self.assertEqual(self.check.call_count, 2)

    def test_restart_dedup_dismiss_and_same_version_replacement(self):
        status = self.run_at('2026-10-12T10:00')
        fingerprint = status['notification_fingerprint']
        self.assertFalse(updates.dismiss_monitor_notice('stale'))
        self.assertFalse(updates.monitor_status()['dismissed'])
        self.assertTrue(updates.dismiss_monitor_notice(fingerprint))
        self.assertTrue(updates.monitor_status()['dismissed'])
        status = self.run_at('2026-10-19T10:00')
        self.assertEqual(len(status['notices']), 1)
        self.assertTrue(status['dismissed'])
        self.row['sha256'] = 'b'*64
        status = self.run_at('2026-10-26T10:00')
        self.assertEqual(len(status['notices']), 1)
        self.assertNotEqual(status['notices'][0]['fingerprint'], fingerprint)
        self.assertFalse(status['dismissed'])

    def test_installed_mismatch_not_repeated_and_first_available_notifies(self):
        self.row['installed_version'] = '7.3.2'
        self.assertFalse(self.run_at('2026-10-12T10:00')['notices'])
        self.assertFalse(self.run_at('2026-10-19T10:00')['notices'])

    def test_initial_builds_are_baselined_then_new_commit_notifies(self):
        self.row['installed_version'] = '7.3.2'
        build = {'client': 'AyuGram', 'build': True, 'version': '', 'tag': 'b'*40,
                 'commit': 'b'*40, 'sha256': '', 'release_url': 'https://github.com/AyuGram/AyuGramDesktop/actions/runs/4'}
        self.extra.return_value = ([build], [])
        self.assertFalse(self.run_at('2026-10-12T10:00')['notices'])
        self.assertFalse(self.run_at('2026-10-19T10:00')['notices'])
        build = dict(build, tag='c'*40, commit='c'*40)
        self.extra.return_value = ([build], [])
        self.assertEqual(len(self.run_at('2026-10-26T10:00')['notices']), 1)

    def test_offline_hour_backoff_preserves_candidates_and_success(self):
        status = self.run_at('2026-10-12T10:00')
        success = status['last_checked']
        self.check.side_effect = lambda: {'clients': [{'client': 'AyuGram', 'error': 'offline'}]}
        status = self.run_at('2026-10-19T10:00')
        self.assertEqual(status['last_checked'], success)
        self.assertEqual(status['candidates'][0]['sha256'], 'a'*64)
        count = self.check.call_count
        self.run_at('2026-10-19T10:59')
        self.assertEqual(self.check.call_count, count)
        self.run_at('2026-10-19T11:00')
        self.assertEqual(self.check.call_count, count+1)
        self.assertEqual(len(status['notices']), 1)

    def test_prerelease_runs_whole_checker_and_partial_sources_retry_hourly(self):
        candidate = dict(self.row, tag='v7.3.3-beta', version='7.3.3-beta')
        self.extra.return_value = ([candidate], ['channel offline'])
        status = self.run_at('2026-10-12T10:00')
        self.assertFalse(status['error'])
        self.assertEqual(status['last_checked'], 0)
        self.assertTrue(any('7.3.3-beta' in n['message'] for n in status['notices']))
        self.run_at('2026-10-12T10:59')
        self.assertEqual(self.check.call_count, 1)
        self.extra.return_value = ([candidate], [])
        status = self.run_at('2026-10-12T11:00')
        self.assertEqual(self.check.call_count, 2)
        self.assertEqual(len(status['notices']), 2)
        self.assertEqual(status['last_checked'], self.stamp('2026-10-12T11:00'))

    def test_new_notice_replaces_previously_dismissed_messages(self):
        status = self.run_at('2026-10-12T10:00')
        old_message = status['notices'][0]['message']
        self.assertTrue(updates.dismiss_monitor_notice(status['notification_fingerprint']))
        build = {'client': 'AyuGram', 'build': True, 'version': '', 'tag': 'd'*40,
                 'commit': 'd'*40, 'sha256': '', 'release_url': 'https://github.com/AyuGram/AyuGramDesktop/actions/runs/9'}
        self.extra.return_value = ([build], [])
        status = self.run_at('2026-10-19T10:00')
        self.assertEqual(len(status['notices']), 1)
        self.assertNotEqual(status['notices'][0]['message'], old_message)

    def test_persistence_failure_and_unsafe_paths_do_not_check(self):
        with mock.patch.object(updates, '_save_monitor', side_effect=OSError('disk full')):
            self.assertIn('disk full', self.run_at('2026-10-12T10:00')['error'])
        self.check.assert_not_called()
        for suffix, hard in [('', False), ('.tmp', False), ('', True), ('.tmp', True)]:
            with self.subTest(suffix=suffix, hard=hard):
                target = Path(self.temp.name) / 'target'
                target.write_text('{}')
                path = Path(str(self.path)+suffix)
                if hard: os.link(target, path)
                else: path.symlink_to(target)
                self.assertIn('Unsafe', self.run_at('2026-10-12T10:00')['error'])
                self.assertEqual(target.read_text(), '{}')
                path.unlink()

    def test_status_never_fetches_and_nonblocking_worker(self):
        updates._MONITOR_LOCK.acquire()
        try:
            self.run_at('2026-10-12T10:00')
            updates.monitor_status()
            self.check.assert_not_called()
        finally:
            updates._MONITOR_LOCK.release()


class OfficialMonitorSourceTests(unittest.TestCase):
    def release(self, tag='v7.3.2-beta', **extra):
        repo = updates.REPOSITORIES['AyuGram']
        return dict({'tag_name': tag, 'prerelease': True, 'assets': [{'name': 'AyuGram.dmg', 'digest': 'sha256:'+'a'*64,
            'browser_download_url': f'https://github.com/{repo}/releases/download/{tag}/AyuGram.dmg'}]}, **extra)

    def test_prerelease_macos_exact_provenance_and_android_filter(self):
        candidate = updates._ayu_candidate(self.release())
        self.assertTrue(candidate['prerelease'])
        self.assertIsNone(updates._ayu_candidate(self.release(draft=True)))
        self.assertIsNone(updates._ayu_candidate(self.release(assets=[{'name': 'AyuGram.apk'}])))
        data = self.release(); data['assets'][0]['browser_download_url'] = 'https://github.com/evil/repo/AyuGram.dmg'
        with self.assertRaises(ValueError): updates._ayu_candidate(data)
        data = self.release(); data['assets'] *= 2
        with self.assertRaises(ValueError): updates._ayu_candidate(data)

    def test_official_channel_and_builds_only(self):
        repo = updates.REPOSITORIES['AyuGram']
        commit = 'b'*40
        artifacts = [{'name': 'macOS-arm64', 'expired': False, 'workflow_run': {'head_sha': commit,
            'repository_id': 1, 'head_repository_id': 1, 'id': 4}}, {'name': 'Android', 'workflow_run': {}},
            {'name': 'macOS', 'expired': True, 'workflow_run': {}}]
        def fetch(url, as_json=True):
            if '/releases?' in url: return [self.release()]
            if '/actions/artifacts?' in url: return {'artifacts': artifacts}
            if url == 'https://t.me/s/AyuGramReleases':
                return f'<a href="https://github.com/{repo}/releases/tag/v7.3.3">Mac</a><a href="https://github.com/AyuGram/AyuGramAndroid/releases/tag/v99.0.0">Android</a>'
            if '/releases/tags/v7.3.3' in url: return self.release('v7.3.3')
            if url.endswith('core/version.h'): return 'constexpr auto AppVersion = 7003000;'
            if url.endswith('storage_file_utilities.cpp'): return 'if (version > AppVersion) return false;'
            self.fail(url)
        with mock.patch.object(updates, '_monitor_fetch', side_effect=fetch):
            candidates, errors = updates._extra_ayu_candidates()
        self.assertFalse(errors)
        self.assertEqual(len(candidates), 3)
        self.assertEqual(candidates[0]['evidence']['app_version'], 7003000)
        self.assertTrue(candidates[0]['evidence']['reader_rejects_newer'])
        self.assertTrue(any(c.get('commit') == commit for c in candidates))

    def test_evidence_skips_historical_and_cached_candidates_and_caps_reads(self):
        releases = [self.release('v7.3.'+str(i)) for i in range(1, 16)]
        fetched = []
        def fetch(url, as_json=True):
            if '/releases?' in url: return releases
            if '/actions/artifacts?' in url: return {'artifacts': []}
            if url == 'https://t.me/s/AyuGramReleases': return ''
            self.fail(url)
        def evidence(candidate):
            fetched.append(candidate['version'])
            return {'app_version': 7003001}
        cached = updates._ayu_candidate(releases[-1])
        cached['evidence'] = {'app_version': 7003001}
        with mock.patch.object(updates, '_monitor_fetch', side_effect=fetch), mock.patch.object(updates, '_candidate_evidence', side_effect=evidence):
            updates._extra_ayu_candidates('7.3.10', {updates._candidate_id(cached): cached})
        self.assertEqual(fetched, ['7.3.14', '7.3.13', '7.3.12', '7.3.11'])
        fetched.clear()
        with mock.patch.object(updates, '_monitor_fetch', side_effect=fetch), mock.patch.object(updates, '_candidate_evidence', side_effect=evidence):
            updates._extra_ayu_candidates('7.3.1')
        self.assertEqual(len(fetched), 5)

    def test_rate_limited_api_uses_official_release_html_without_binary_download(self):
        repo = updates.REPOSITORIES['AyuGram']
        urls = []
        def response(url, payload):
            value = io.BytesIO(payload.encode()); value.geturl = lambda: url
            return value
        def fetch(request, timeout):
            url = request.full_url; urls.append(url)
            if url.startswith('https://api.github.com/'):
                raise updates.urllib.error.HTTPError(url, 403, 'rate limited', {}, None)
            if url == f'https://github.com/{repo}/releases':
                return response(url, f'<a href="/{repo}/releases/tag/v7.3.3-beta">new</a><a href="/{repo}/releases/tag/v7.2.9">old</a>')
            if url == f'https://github.com/{repo}/releases/expanded_assets/v7.3.3-beta':
                return response(url, f'<li><a href="/{repo}/releases/download/v7.3.3-beta/AyuGram.dmg">mac</a><clipboard-copy value="sha256:{"a"*64}"></clipboard-copy></li>')
            if url == 'https://t.me/s/AyuGramReleases':
                return response(url, f'<a href="https://github.com/{repo}/releases/tag/v7.2.9">old</a>')
            self.fail('Unexpected fetch: '+url)
        with mock.patch.object(updates.urllib.request, 'urlopen', side_effect=fetch), mock.patch.object(updates, '_candidate_evidence', return_value={'app_version': 7003003}):
            candidates, errors = updates._extra_ayu_candidates('7.3.1')
        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0]['prerelease'])
        self.assertEqual(candidates[0]['sha256'], 'a'*64)
        self.assertEqual(len(errors), 1)
        self.assertIn('public macOS build metadata unavailable', errors[0])
        self.assertFalse(any('/releases/download/' in url or '/releases/tags/v7.2.9' in url for url in urls))

    def test_channel_skips_known_stable_and_falls_back_for_new_tag(self):
        repo = updates.REPOSITORIES['AyuGram']
        fetched = []
        stable = updates._ayu_candidate(self.release('v7.3.2'))
        def fetch(url, as_json=True):
            fetched.append(url)
            if '/releases?' in url: return []
            if '/actions/artifacts?' in url: return {'artifacts': []}
            if url == 'https://t.me/s/AyuGramReleases':
                return ''.join(f'<a href="https://github.com/{repo}/releases/tag/{tag}">release</a>' for tag in ['v7.2.9', 'v7.3.2', 'v7.3.3-beta'])
            if '/releases/tags/v7.3.3-beta' in url:
                raise updates.urllib.error.HTTPError(url, 429, 'rate limited', {}, None)
            self.fail(url)
        with mock.patch.object(updates, '_monitor_fetch', side_effect=fetch), mock.patch.object(updates, '_official_release_page', return_value=self.release('v7.3.3-beta')) as page, mock.patch.object(updates, '_candidate_evidence', return_value={'app_version': 7003003}):
            candidates, errors = updates._extra_ayu_candidates('7.3.1', {updates._candidate_id(stable): stable})
        page.assert_called_once_with(repo, 'v7.3.3-beta')
        self.assertEqual(len(candidates), 1)
        self.assertFalse(errors)
        self.assertFalse(any('/releases/tags/v7.3.2' in url or '/releases/tags/v7.2.9' in url for url in fetched))

    def test_metadata_bounds_and_redirects(self):
        for payload, final in [(b'x'*(2*1024*1024+1), 'https://api.github.com/repos/AyuGram/AyuGramDesktop/releases'),
                               (b'[]', 'https://evil.example/releases')]:
            response = io.BytesIO(payload); response.geturl = lambda: final
            with mock.patch.object(updates.urllib.request, 'urlopen', return_value=response):
                with self.assertRaises(ValueError):
                    updates._monitor_fetch('https://api.github.com/repos/AyuGram/AyuGramDesktop/releases')

    def test_optional_sources_unavailable_preserves_basic_monitor(self):
        with mock.patch.object(updates, '_monitor_fetch', side_effect=OSError('offline')):
            candidates, errors = updates._extra_ayu_candidates()
        self.assertEqual(candidates, [])
        self.assertEqual(len(errors), 3)
