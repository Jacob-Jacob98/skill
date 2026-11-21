"""Offline preflight concurrency, routing, incremental output, and safety tests."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import preflight


class PreflightTests(unittest.TestCase):
    def setUp(self):
        test_root = Path(__file__).resolve().parent.parent / 'optimized-tests'
        test_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='preflight-test-', dir=test_root)
        self.assertTrue(Path(self.temp.name).resolve().is_relative_to(test_root.resolve()))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = self.root / 'input.json'
        self.work = self.root / 'work'
        redirect = contextlib.redirect_stdout(io.StringIO())
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def args(self, count=15, **overrides):
        tracks = [{'index': i + 1, 'title': f'track-{i + 1}', 'stage': 'wedding', 'artists': ['artist']}
                  for i in range(count)]
        self.manifest.write_text(json.dumps({'tracks': tracks}), encoding='utf-8')
        values = dict(manifest=self.manifest, work=self.work, sources='QQ,网易云',
                      deps=None, limit=3, timeout=20, workers=4)
        values.update(overrides)
        return argparse.Namespace(**values)

    @staticmethod
    def success(job, *unused):
        return {'ok': True, 'records': [{'song_name': job['query'], 'source': job['source'], 'identifier': 'x'}]}

    def read(self, name):
        return json.loads((self.work / name).read_text(encoding='utf-8'))

    def test_fifteen_tracks_overlap_and_respect_four_and_fifteen_worker_limits(self):
        for workers in (4, 15):
            with self.subTest(workers=workers):
                self.work = self.root / f'work-{workers}'
                args = self.args(workers=workers)
                gate = threading.Barrier(workers)
                lock = threading.Lock()
                active = peak = calls = 0

                def fake(job, deps, timeout, folder):
                    nonlocal active, peak, calls
                    self.assertEqual(job['action'], 'metadata')
                    self.assertEqual(timeout, 20)
                    with lock:
                        active += 1
                        peak = max(peak, active)
                        calls += 1
                        ordinal = calls
                    try:
                        if ordinal <= workers:
                            gate.wait(timeout=5)
                        time.sleep(0.015)
                        return self.success(job)
                    finally:
                        with lock:
                            active -= 1

                with patch.object(preflight.music, 'run_job', side_effect=fake):
                    self.assertEqual(preflight.run(args), 0)
                report = self.read('preflight-report.json')
                self.assertEqual(calls, 15)
                self.assertEqual(peak, workers)
                self.assertEqual(report['peak_concurrent_requests'], workers)
                self.assertLessEqual(report['peak_concurrent_tracks'], workers)
                self.assertEqual(report['source_peak_concurrency']['QQ'], workers)
                self.assertEqual(report['concurrency_scope'], 'this_preflight_batch_only')
                self.assertTrue(report['complete'])
                self.assertGreater(report['wall_seconds'], 0)
                self.assertTrue(all(r['elapsed_seconds'] > 0 and r['sources'][0]['elapsed_seconds'] > 0
                                    for r in report['tracks']))
                self.assertEqual([r['index'] for r in report['tracks']], list(range(1, 16)))

    def test_slow_first_track_does_not_block_fast_track_files(self):
        args = self.args(count=2, workers=2)
        slow_started, release_slow, fast_saved = threading.Event(), threading.Event(), threading.Event()
        failures = []
        original_save = preflight.pipeline.save

        def fake(job, *unused):
            if job['query'].startswith('track-1 '):
                slow_started.set()
                if not release_slow.wait(5):
                    raise TimeoutError('test release missing')
            else:
                self.assertTrue(slow_started.wait(5))
            return self.success(job)

        def observe(path, payload):
            original_save(path, payload)
            if path.name == 'manifest.pending.json' and [r['index'] for r in payload['tracks']] == [2]:
                row = self.read('manifest.pending.json')['tracks'][0]
                self.assertTrue(Path(row['session']).is_file())
                self.assertFalse(row['confirmed'])
                fast_saved.set()

        def execute():
            try:
                preflight.run(args)
            except BaseException as exc:
                failures.append(exc)

        with patch.object(preflight.music, 'run_job', side_effect=fake), patch.object(preflight.pipeline, 'save', side_effect=observe):
            thread = threading.Thread(target=execute)
            thread.start()
            try:
                self.assertTrue(fast_saved.wait(5), 'Fast result did not reach disk while first track was blocked')
                self.assertTrue(thread.is_alive())
            finally:
                release_slow.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual([r['index'] for r in self.read('manifest.pending.json')['tracks']], [1, 2])

    def test_qq_priority_fallback_on_empty_failure_and_exception(self):
        args = self.args(count=5, sources='网易云,QQ')
        calls = {i: [] for i in range(1, 6)}

        def fake(job, *unused):
            number = int(job['query'].split()[0].split('-')[1])
            calls[number].append(job['source'])
            if job['source'] == 'QQMusicClient':
                if number == 1:
                    return {'ok': True, 'records': []}
                if number == 2:
                    return {'ok': False, 'error_kind': 'timeout', 'error': 'test timeout'}
                if number == 3:
                    raise RuntimeError('source exception must stay isolated')
            if number == 4:
                return {'ok': False, 'error': 'unavailable'}
            return self.success(job)

        with patch.object(preflight.music, 'run_job', side_effect=fake):
            preflight.run(args)
        self.assertEqual(calls, {1: list(preflight.PRIORITY), 2: list(preflight.PRIORITY),
                                 3: list(preflight.PRIORITY), 4: list(preflight.PRIORITY),
                                 5: ['QQMusicClient']})
        reviews = self.read('review.json')['tracks']
        self.assertEqual([r['status'] for r in reviews], ['needs_review'] * 3 + ['needs_retry', 'needs_review'])
        self.assertEqual(reviews[1]['sources'][0]['error_kind'], 'timeout')
        self.assertEqual(reviews[2]['sources'][0]['error_kind'], 'source_error')

    def test_failed_track_does_not_cancel_other_futures(self):
        args = self.args(count=3)
        original = preflight.search_track

        def faulty(track, *remaining):
            if track['index'] == 2:
                raise RuntimeError('unexpected track exception')
            return original(track, *remaining)

        with patch.object(preflight, 'search_track', side_effect=faulty), patch.object(preflight.music, 'run_job', side_effect=self.success):
            preflight.run(args)
        review = self.read('review.json')['tracks']
        self.assertEqual([r['status'] for r in review], ['needs_review', 'failed', 'needs_review'])
        self.assertEqual(review[1]['error_kind'], 'track_error')
        self.assertTrue(all(Path(r['session']).is_file() for r in review))

    def test_fresh_unique_sessions_no_inherited_approval_and_original_manifest_order(self):
        args = self.args(count=3)
        data = json.loads(self.manifest.read_text(encoding='utf-8'))
        data['tracks'] = [data['tracks'][2], data['tracks'][0], data['tracks'][1]]
        for row in data['tracks']:
            row.update(confirmed=True, candidate_id=99, source_file='old.mp3', session='old.json', confirmed_by='old')
        self.manifest.write_text(json.dumps(data), encoding='utf-8')
        sessions = set()
        for number in (1, 2):
            self.work = self.root / f'fresh-{number}'
            args.work = self.work
            with patch.object(preflight.music, 'run_job', side_effect=self.success):
                preflight.run(args)
            rows = self.read('manifest.pending.json')['tracks']
            self.assertEqual([r['index'] for r in rows], [3, 1, 2])
            self.assertEqual([r['index'] for r in self.read('review.json')['tracks']], [3, 1, 2])
            for row in rows:
                self.assertIs(row['confirmed'], False)
                self.assertFalse({'candidate_id', 'source_file', 'confirmed_by'} & row.keys())
                self.assertNotIn(row['session'], sessions)
                sessions.add(row['session'])
                session = json.loads(Path(row['session']).read_text(encoding='utf-8'))
                self.assertEqual(session['schema'], 1)
                self.assertIs(session['candidates'][0]['confirmed'], False)

    def test_existing_work_and_claimed_work_are_preserved(self):
        args = self.args(count=1)
        self.work.mkdir()
        sentinel = self.work / 'manifest.pending.json'
        sentinel.write_text('{"tracks":[{"confirmed":true}]}', encoding='utf-8')
        before = sentinel.read_bytes()
        with patch.object(preflight.music, 'run_job') as job:
            with self.assertRaisesRegex(ValueError, 'not empty'):
                preflight.run(args)
            job.assert_not_called()
        self.assertEqual(sentinel.read_bytes(), before)
        self.assertFalse((self.work / '.preflight.lock').exists())
        args.work = self.root / 'claimed'
        args.work.mkdir()
        lock = args.work / '.preflight.lock'
        lock.write_text('other process', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'already claimed'):
            preflight.run(args)
        self.assertEqual(lock.read_text(encoding='utf-8'), 'other process')

    def test_edits_to_pending_during_run_are_not_overwritten(self):
        args = self.args(count=2, workers=1)
        changed = {'tracks': [{'confirmed': True, 'candidate_id': 77}]}
        original_save = preflight.pipeline.save
        edited = False

        def save_then_review(path, payload):
            nonlocal edited
            original_save(path, payload)
            if path.name == 'preflight-report.json' and payload['completed'] == 1 and not edited:
                (self.work / 'manifest.pending.json').write_text(json.dumps(changed), encoding='utf-8')
                edited = True

        with patch.object(preflight.music, 'run_job', side_effect=self.success), patch.object(preflight.pipeline, 'save', side_effect=save_then_review):
            preflight.run(args)
        self.assertEqual(self.read('manifest.pending.json'), changed)
        generated = Path(self.read('preflight-report.json')['pending_manifest'])
        self.assertNotEqual(generated.name, 'manifest.pending.json')
        rows = json.loads(generated.read_text(encoding='utf-8'))['tracks']
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r['confirmed'] is False for r in rows))

    def test_rate_limited_source_defers_queued_tracks_without_sleep_or_retry(self):
        args = self.args(count=15, workers=1)
        calls = []

        def fake(job, *unused):
            calls.append(job['source'])
            if job['source'] == 'QQMusicClient':
                return {'ok': False, 'error_kind': 'rate_limited', 'http_status': 429,
                        'retry_after_seconds': 60, 'error': 'rate limited'}
            return self.success(job)

        with patch.object(preflight.music, 'run_job', side_effect=fake), patch.object(preflight.time, 'sleep', side_effect=AssertionError('must not sleep')):
            preflight.run(args)
        self.assertEqual(calls.count('QQMusicClient'), 1)
        self.assertEqual(calls.count('NeteaseMusicClient'), 15)
        review = self.read('review.json')['tracks']
        self.assertEqual(review[0]['sources'][0]['http_status'], 429)
        self.assertEqual(review[0]['sources'][0]['retry_after_seconds'], 60)
        self.assertTrue(all(r['sources'][0]['status'] == 'deferred' for r in review[1:]))
        self.assertTrue(all(r['status'] == 'needs_review' for r in review))

    def test_rate_limit_default_and_expired_cooldown(self):
        for invalid in (None, '60', -1, float('nan'), True):
            with self.subTest(retry_after=invalid):
                state = preflight.BatchState()
                with patch.object(preflight.time, 'perf_counter', return_value=100):
                    self.assertEqual(state.begin_request('QQMusicClient'), 0)
                    state.finish_request('QQMusicClient', {'http_status': 429, 'retry_after_seconds': invalid})
                    self.assertEqual(state.begin_request('QQMusicClient'), 10)
                with patch.object(preflight.time, 'perf_counter', return_value=111):
                    self.assertEqual(state.begin_request('QQMusicClient'), 0)
                    state.finish_request('QQMusicClient', {'ok': True})

    def test_rate_limit_keeps_inflight_bounded_and_skips_queued_source_requests(self):
        args = self.args(count=15, workers=4)
        gate, lock = threading.Barrier(4), threading.Lock()
        calls = []
        active = peak = 0

        def fake(job, *unused):
            nonlocal active, peak
            with lock:
                calls.append(job['source'])
                active += 1
                peak = max(peak, active)
            try:
                if job['source'] == 'QQMusicClient':
                    gate.wait(timeout=5)
                    return {'ok': False, 'http_status': 429, 'retry_after_seconds': 60}
                return {'ok': True, 'records': []}
            finally:
                with lock:
                    active -= 1

        with patch.object(preflight.music, 'run_job', side_effect=fake):
            preflight.run(args)
        self.assertEqual(calls.count('QQMusicClient'), 4)
        self.assertEqual(calls.count('NeteaseMusicClient'), 15)
        self.assertEqual(peak, 4)
        report = self.read('preflight-report.json')
        self.assertEqual(report['peak_concurrent_requests'], 4)
        self.assertTrue(all(r['status'] == 'needs_retry' for r in report['tracks']))
        self.assertTrue(all(r['sources'][0]['status'] == 'deferred' for r in report['tracks'][4:]))

    def test_not_found_means_all_sources_returned_successfully_empty(self):
        args = self.args(count=1)
        with patch.object(preflight.music, 'run_job', return_value={'ok': True, 'records': []}):
            preflight.run(args)
        row = self.read('review.json')['tracks'][0]
        self.assertEqual(row['status'], 'not_found')
        self.assertEqual([s['status'] for s in row['sources']], ['empty', 'empty'])

    def test_cli_defaults_validation_and_custom_timeout(self):
        with patch.object(preflight, 'run', return_value=0) as run:
            self.assertEqual(preflight.main(['--manifest', 'input.json', '--work', 'new']), 0)
            args = run.call_args.args[0]
            self.assertEqual((args.workers, args.timeout), (4, 20))
        with contextlib.redirect_stderr(io.StringIO()):
            for invalid in ('0', '16'):
                with self.assertRaises(SystemExit):
                    preflight.main(['--manifest', 'input.json', '--work', 'new', '--workers', invalid])
        args = self.args(count=1, timeout=7)
        with patch.object(preflight.music, 'run_job', side_effect=self.success) as job:
            preflight.run(args)
        self.assertEqual(job.call_args.args[2], 7)

    def test_unsupported_sources_rejected_without_jobs(self):
        args = self.args(count=1, sources='酷狗')
        with patch.object(preflight.music, 'run_job') as job:
            with self.assertRaisesRegex(ValueError, 'only supports'):
                preflight.run(args)
            job.assert_not_called()
        self.assertFalse(self.work.exists())


def main():
    unittest.main()


if __name__ == '__main__':
    main()
