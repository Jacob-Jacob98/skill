"""Verify actual audio quality and preview rejection using small FFmpeg fixtures."""
from pathlib import Path
import argparse
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import imageio_ffmpeg
import music


class DownloadQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent / 'quality-tests'
        root.mkdir(exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(dir=root)
        cls.root = Path(cls.temp.name)
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        for label, settings in [('320.mp3', ['-c:a','libmp3lame','-b:a','320k']),
                                ('128.mp3', ['-c:a','libmp3lame','-b:a','128k']),
                                ('lossless.flac', ['-c:a','flac'])]:
            subprocess.run([ffmpeg,'-v','error','-f','lavfi','-i',
                'sine=frequency=440:duration=2','-ar','44100','-ac','2',
                *settings, str(cls.root/label)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_actual_320_is_recognized(self):
        info = music.inspect_download(self.root/'320.mp3', 2)
        self.assertEqual(info['source_format'], 'mp3')
        self.assertEqual(info['source_bitrate'], 320000)
        self.assertTrue(info['source_is_mp3_320'])
        self.assertIsNone(info['quality_fallback'])

    def test_128_is_never_reported_as_320(self):
        info = music.inspect_download(self.root/'128.mp3', 2)
        self.assertAlmostEqual(info['source_bitrate'], 128000, delta=100)
        self.assertFalse(info['source_is_mp3_320'])
        self.assertEqual(info['quality_fallback'], 'mp3_other_bitrate')

    def test_false_mp3_suffix_does_not_hide_flac(self):
        path = self.root/'false.mp3'
        shutil.copyfile(self.root/'lossless.flac', path)
        info = music.inspect_download(path, 2)
        self.assertEqual(info['source_format'], 'flac')
        self.assertFalse(info['source_is_mp3_320'])
        self.assertEqual(info['quality_fallback'], 'lossless_source')

    def test_preview_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'duration differs'):
            music.inspect_download(self.root/'320.mp3', 240)

    def run_fake_download(self, path, phase, cached=False):
        with tempfile.TemporaryDirectory(dir=self.root) as temp:
            root = Path(temp)
            session, out = root/'session.json', root/'out'
            out.mkdir()
            record = dict(source='QQMusicClient', identifier='same-id', song_name='Test',
                          singers=['Artist'], metadata_only=True, duration_s=2)
            music.write_json(session, {'schema':1, 'created':time.time(),
                'candidates':[{'id':1,'record':record}]})
            if cached:
                old = out/'old.mp3'
                shutil.copyfile(self.root/'128.mp3', old)
                music.write_json(out/'.music-download-history.json', {music.record_key(record):{
                    'path':str(old), 'sha256':music.digest(old), 'acquisition_policy':'mp3-first-v3',
                    'audio_info':{'source_is_mp3_320':True}}})
            def fake(job, deps, timeout, work):
                target = Path(work)/'audio'/'source.mp3'
                target.parent.mkdir(parents=True)
                shutil.copyfile(path, target)
                return dict(ok=True, paths=[str(target)], resolution_phase=phase)
            args = argparse.Namespace(session=session, out=out, ids='1', deps=None, timeout=3)
            with patch.object(music, 'run_job', side_effect=fake) as called, patch.object(music, 'emit'):
                code = music.download(args)
            report = music.read_json(out/'download-report.json')['results'][0]
            return code, report, called.call_count

    def test_quality_change_after_probe_is_rejected(self):
        code, report, calls = self.run_fake_download(self.root/'128.mp3', 'mp3_320')
        self.assertEqual(code, 2)
        self.assertEqual(report['status'], 'failed')
        self.assertIn('quality changed', report['error'])

    def test_missing_phase_cannot_be_marked_verified(self):
        code, report, calls = self.run_fake_download(self.root/'128.mp3', None)
        self.assertEqual(code, 2)
        self.assertIn('quality policy', report['error'])

    def test_low_bitrate_cached_file_is_rechecked(self):
        code, report, calls = self.run_fake_download(self.root/'320.mp3', 'mp3_320', cached=True)
        self.assertEqual(code, 0)
        self.assertEqual(calls, 1)
        self.assertEqual(report['status'], 'downloaded')
        self.assertTrue(report['source_is_mp3_320'])


if __name__ == '__main__':
    unittest.main()
