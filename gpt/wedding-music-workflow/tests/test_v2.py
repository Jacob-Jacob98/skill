"""Offline tests for independent acquisition/processing and safe source failures."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import metadata
import music
import pipeline


def test_directory():
    root = Path(__file__).resolve().parent.parent/'optimized-tests-v2'
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix='v2-', dir=root)


class PipelineTests(unittest.TestCase):
    def test_serial_download_overlaps_processing_and_failure_does_not_stop_queue(self):
        with test_directory() as temp:
            root = Path(temp)
            tracks = [dict(index=i, stage='环节', title=f'歌曲{i}', artists=['甲/乙'],
                           confirmed=i != 4, session='session.json', candidate_id=i)
                      for i in range(1, 5)]
            manifest = root/'manifest.json'
            pipeline.save(manifest, {'tracks':tracks})
            args = argparse.Namespace(manifest=manifest, work=root/'batch', deps=None,
                ffmpeg='unused', download_workers=1, process_workers=1, fresh=False,
                validation='strict', download_timeout=10)
            encoding_first, downloading_second = threading.Event(), threading.Event()
            active, peak, order = 0, 0, []
            guard = threading.Lock()

            def fake_download(cmd, **kwargs):
                nonlocal active, peak
                folder = Path(cmd[cmd.index('--out')+1])
                n = int(folder.parent.name)
                with guard:
                    active += 1
                    peak = max(peak, active)
                    order.append(n)
                try:
                    if n == 2:
                        self.assertTrue(encoding_first.wait(5), 'Processing did not overlap the next download')
                        downloading_second.set()
                    if n == 3:
                        return SimpleNamespace(returncode=1)
                    source = folder/'source.mp3'
                    source.parent.mkdir(parents=True, exist_ok=True)
                    source.write_bytes(b'source')
                    pipeline.save(folder/'download-report.json', {'results':[{
                        'status':'downloaded', 'path':str(source), 'resolve_seconds':0.1,
                        'download_seconds':0.2, 'resolution_strategy':'test'}]})
                    return SimpleNamespace(returncode=0)
                finally:
                    with guard:
                        active -= 1

            def fake_encode(source, dest, job, *unused):
                if job['index'] == 1:
                    encoding_first.set()
                    self.assertTrue(downloading_second.wait(5), 'Download waited for the entire encode')
                dest.write_bytes(b'encoded')
                return dict(duration=3, full_decode=True, processing_seconds=0.1,
                            validation_seconds=0.01, processing_mode='encode', bitrate=320000)

            with patch.object(pipeline.subprocess, 'run', side_effect=fake_download), \
                    patch.object(pipeline, 'encode', side_effect=fake_encode), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pipeline.run(args), 2)
            report = pipeline.load(args.work/'report.json')
            self.assertEqual(order, [1, 2, 3])
            self.assertEqual(peak, 1)
            self.assertEqual([r['status'] for r in report['results']], ['verified','verified','failed','pending'])
            self.assertEqual(report['peak_downloads'], 1)
            self.assertEqual(report['peak_processing'], 1)
            self.assertFalse((args.work/'.run.lock').exists())
            self.assertTrue((args.work/'jobs/2/result.json').exists())

    def test_same_batch_cannot_run_or_publish_concurrently(self):
        with test_directory() as temp:
            args = SimpleNamespace(work=Path(temp)/'batch')
            with pipeline.batch_lock(args.work):
                with self.assertRaisesRegex(ValueError, 'already in use'):
                    pipeline.run(args)
                with self.assertRaisesRegex(ValueError, 'already in use'):
                    pipeline.publish(args)
            self.assertFalse((args.work/'.run.lock').exists())


class SourceErrorTests(unittest.TestCase):
    def test_worker_preserves_rate_limit_without_private_url(self):
        with test_directory() as temp:
            root = Path(temp)
            job = root/'job.json'
            result = root/'result.json'
            music.write_json(job, dict(action='metadata', source='QQMusicClient',
                                      query='song', result=str(result)))
            with patch.object(metadata, 'search', side_effect=metadata.SourceHTTPError(429, '12')):
                music.worker(job)
            data = music.read_json(result)
            self.assertEqual(data['error_kind'], 'rate_limited')
            self.assertEqual(data['retry_after_seconds'], 12)
            self.assertEqual(data['http_status'], 429)
            self.assertNotIn('https://', json.dumps(data))

    def test_timeout_is_a_typed_failure(self):
        with test_directory() as temp:
            with patch.object(music.subprocess, 'run', side_effect=subprocess.TimeoutExpired('private-command', 1)):
                result = music.run_job({'action':'metadata'}, None, 1, Path(temp))
            self.assertEqual(result['error_kind'], 'timeout')
            self.assertNotIn('private-command', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
