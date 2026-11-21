"""Offline resolver tests: synthetic audio, no music service or installed adapter calls."""
import unittest
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import metadata


def mp3_sample(kbps=320, frames=40):
    rates = [0,32,40,48,56,64,80,96,112,128,160,192,224,256,320]
    header = (0x7ff << 21) | (3 << 19) | (1 << 17) | (1 << 16) | (rates.index(kbps) << 12)
    frame = header.to_bytes(4, 'big') + bytes(144 * kbps * 1000 // 44100 - 4)
    return frame * frames


def flac_sample(bits=16, samplerate=44100, channels=2, seconds=200):
    packed = (samplerate << 44) | ((channels-1) << 41) | ((bits-1) << 36) | (seconds*samplerate)
    info = bytes(10) + packed.to_bytes(8, 'big') + bytes(16)
    return b'fLaC' + b'\x80\x00\x00\x22' + info + bytes(100)


class Response:
    def __init__(self, url, data=b'', payload=None, status=200, headers=None):
        self.url, self.data, self.payload, self.status_code = url, data, payload, status
        self.headers = headers or {}
        self.closed, self.emitted = False, 0

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ValueError('fixture unavailable')

    def json(self):
        return self.payload or {}

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.data), chunk_size):
            part = self.data[offset:offset+chunk_size]
            self.emitted += len(part)
            yield part

    def close(self):
        self.closed = True


class Transport:
    def __init__(self, audio, calls, ignore_range=False):
        self.audio, self.calls, self.ignore_range = audio, calls, ignore_range
        self.responses, self.request_options = [], []

    def get(self, url, **kwargs):
        self.request_options.append(kwargs)
        if 'vkeys.cn' in url:
            key = 'vkeys_' + str(kwargs['params']['quality'])
            self.calls.append(key)
            response = Response(url, payload={'data':{'url':'https://audio/' + key}} if key in self.audio else {})
        else:
            key = url.rsplit('/', 1)[-1]
            if key not in self.audio:
                response = Response(url, status=404)
            else:
                data = self.audio[key]
                if self.ignore_range:
                    response = Response(url, data, headers={'Content-Length':str(len(data))})
                else:
                    first, last = kwargs['headers']['Range'].removeprefix('bytes=').split('-')
                    first, last = int(first), int(last)
                    part = data[first:last+1]
                    response = Response(url, part, status=206, headers={
                        'Content-Range':f'bytes {first}-{first+len(part)-1}/{len(data)}',
                        'Content-Length':str(len(part))})
        self.responses.append(response)
        return response

    post = get


class Song:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def todict(self):
        return dict(self.__dict__)


def fixture(source, audio, mismatch=None):
    calls = []
    transport = Transport(audio, calls)
    qq = source == 'QQMusicClient'
    module = SimpleNamespace(requests=transport, MUSIC_QUALITIES=['jymaster', 'exhigh'],
        SongFileType=SimpleNamespace(SORTED_QUALITIES=SimpleNamespace(value=[('AI00','.flac'),('M800','.mp3'),('F000','.flac')])))

    class Client:
        default_download_headers = {}
        default_download_cookies = {}

        def __init__(self, **kwargs):
            if not kwargs.get('maintain_session'):
                raise AssertionError('Real adapter would discard the byte probe between requests')

        def get(self, *args, **kwargs):
            return transport.get(*args, **kwargs)

        post = get

        def run(self, key):
            calls.append(key)
            if key not in audio:
                return Song(source=source)
            status = self.audio_link_tester.test('https://audio/' + key)
            return Song(identifier='wrong' if mismatch == key else 'chosen', download_url_status=status)

        def _parsewithofficialapiv1(self, **kwargs):
            quality = module.SongFileType.SORTED_QUALITIES.value[0][0] if qq else module.MUSIC_QUALITIES[0]
            return self.run('official_' + quality)

        def _parsewiththirdpartapis(self, **kwargs):
            raise AssertionError('Unbounded legacy dispatcher must never be used')

    for name in ['lzmhhh', 'xunhuisi', 'mikus', 'qqovo', 'cy', 'tmetu', 'xuanluoge']:
        def method(self, _name=name, **kwargs):
            key = _name if qq else _name + '_' + module.MUSIC_QUALITIES[0]
            return self.run(key)
        setattr(Client, '_parsewith'+name+'api', method)
    return module, Client, transport, calls


RECORD = {'source':'QQMusicClient', 'identifier':'chosen', 'song_name':'Selected song',
          'singers':['Selected singer'], 'album':'Selected album', 'duration_s':200, 'duration':'3:20',
          'raw_data':{'search':{'mid':'chosen'}}}


class ProbeTests(unittest.TestCase):
    def test_real_frames_determine_bitrate(self):
        self.assertEqual(metadata.inspect_audio_sample(mp3_sample())['bitrate'], 320000)
        self.assertEqual(metadata.inspect_audio_sample(mp3_sample(128))['bitrate'], 128000)
        self.assertIsNone(metadata.inspect_audio_sample(b'ID3' + bytes(300)))

    def test_flac_bytes_override_an_mp3_url(self):
        transport = Transport({'fake.mp3':flac_sample(24, 96000)}, [])
        status = metadata._AudioProbe(transport, metadata._Budget()).test('https://audio/fake.mp3')
        self.assertEqual(status['ext'], 'flac')
        self.assertEqual(status['probe_bits_per_sample'], 24)
        self.assertFalse(metadata._ordinary_flac(status))

    def test_range_probe_caps_read_even_when_server_sends_whole_file(self):
        transport = Transport({'large':mp3_sample(frames=200)}, [], ignore_range=True)
        probe = metadata._AudioProbe(transport, metadata._Budget())
        status = probe.test('https://audio/large')
        self.assertEqual(status['probe_sample_bytes'], probe.MAX_BYTES)
        self.assertEqual(transport.responses[0].emitted, probe.MAX_BYTES)
        self.assertTrue(transport.responses[0].closed)
        self.assertTrue(transport.request_options[0]['stream'])
        self.assertLessEqual(max(transport.request_options[0]['timeout']), 2)

    def test_partial_content_reports_whole_size_and_cache_avoids_request(self):
        audio = mp3_sample(frames=100)
        transport = Transport({'mp3':audio}, [])
        probe = metadata._AudioProbe(transport, metadata._Budget())
        first = probe.test('https://audio/mp3')
        second = probe.test('https://audio/mp3')
        self.assertEqual(first['file_size_bytes'], len(audio))
        self.assertEqual(first, second)
        self.assertEqual(len(transport.responses), 1)

    def test_large_id3_uses_only_bounded_second_range(self):
        length = 50000
        synchsafe = bytes((length >> shift) & 127 for shift in (21,14,7,0))
        data = b'ID3\x04\x00\x00' + synchsafe + bytes(length) + mp3_sample()
        transport = Transport({'tagged':data}, [])
        probe = metadata._AudioProbe(transport, metadata._Budget())
        status = probe.test('https://audio/tagged')
        self.assertEqual(status['probe_bitrate'], 320000)
        self.assertEqual(len(transport.responses), 2)
        self.assertLessEqual(status['probe_sample_bytes'], probe.MAX_BYTES + probe.TAG_FOLLOWUP_BYTES)
        self.assertEqual(transport.request_options[1]['headers']['Range'], 'bytes=50010-58201')

    def test_mp3_in_large_id3_cannot_be_claimed_if_server_ignores_range(self):
        length = 50000
        data = b'ID3\x04\x00\x00' + bytes((length >> shift)&127 for shift in (21,14,7,0)) + bytes(length) + mp3_sample()
        transport = Transport({'tagged':data}, [], ignore_range=True)
        with self.assertRaises(ValueError):
            metadata._AudioProbe(transport, metadata._Budget()).test('https://audio/tagged')
        self.assertEqual(transport.responses[1].emitted, 0)
        self.assertTrue(all(response.closed for response in transport.responses))

    def test_ffmpeg_cbr320_and_large_real_id3_tag(self):
        try:
            import imageio_ffmpeg
            from mutagen.id3 import ID3, TIT2
        except ImportError:
            self.skipTest('Optional FFmpeg/Mutagen fixture tools unavailable')
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            self.assertTrue(Path(temp).resolve().is_relative_to(Path(__file__).resolve().parent))
            path = Path(temp) / 'real-320.mp3'
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error',
                '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3', '-ar', '44100', '-ac', '2',
                '-c:a', 'libmp3lame', '-b:a', '320k', str(path)], check=True, timeout=20)
            original = path.read_bytes()
            self.assertEqual(metadata.inspect_audio_sample(original[:32768])['bitrate'], 320000)
            tags = ID3(path)
            tags.add(TIT2(encoding=3, text='Real CBR fixture with large metadata'))
            tags.save(path, padding=lambda info:70000)
            transport = Transport({'tagged':path.read_bytes()}, [])
            probe = metadata._AudioProbe(transport, metadata._Budget())
            status = probe.test('https://audio/tagged')
            self.assertEqual(status['probe_bitrate'], 320000)
            self.assertEqual(status['probe_bitrate_max'], 320000)
            self.assertEqual(len(transport.responses), 2)
            self.assertLessEqual(status['probe_sample_bytes'], 40960)

    def test_ffmpeg_128_is_measured_as_128(self):
        try:
            import imageio_ffmpeg
        except ImportError:
            self.skipTest('Optional FFmpeg fixture tool unavailable')
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            self.assertTrue(Path(temp).resolve().is_relative_to(Path(__file__).resolve().parent))
            path = Path(temp) / 'real-128.mp3'
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error',
                '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3', '-ar', '44100', '-ac', '2',
                '-c:a', 'libmp3lame', '-b:a', '128k', str(path)], check=True, timeout=20)
            self.assertEqual(metadata.inspect_audio_sample(path.read_bytes()[:32768])['bitrate'], 128000)


class ResolveTests(unittest.TestCase):
    def resolve_fixture(self, audio, source='QQMusicClient', mismatch=None):
        module, client, transport, calls = fixture(source, audio, mismatch)
        old_requests, old_qualities = module.requests, module.SongFileType if source.startswith('QQ') else module.MUSIC_QUALITIES
        record = dict(RECORD, source=source)
        with patch.object(metadata, '_load_adapter', return_value=(module, client, Song)):
            result = metadata.resolve(record, '.')
        self.assertIs(module.requests, old_requests)
        self.assertIs(module.SongFileType if source.startswith('QQ') else module.MUSIC_QUALITIES, old_qualities)
        return result, calls, transport

    def test_flac_from_hq_does_not_preempt_later_mp3(self):
        result, calls, _ = self.resolve_fixture({'vkeys_8':flac_sample(), 'lzmhhh':mp3_sample()})
        self.assertEqual(result['ext'], 'mp3')
        self.assertEqual(result['identifier'], 'chosen')
        self.assertEqual(result['raw_data']['resolution_phase'], 'mp3_320')
        self.assertEqual(calls, ['official_M800', 'vkeys_8', 'lzmhhh'])
        self.assertNotIn('official_F000', calls)

    def test_cached_flac_used_only_after_every_configured_mp3_route(self):
        result, calls, transport = self.resolve_fixture({'vkeys_8':flac_sample()})
        self.assertEqual(result['raw_data']['resolution_phase'], 'ordinary_flac')
        self.assertEqual(calls, ['official_M800', 'vkeys_8', 'lzmhhh', 'xunhuisi', 'mikus'])
        self.assertEqual(sum('/audio/' in response.url for response in transport.responses), 1)

    def test_low_mp3_does_not_beat_ordinary_flac(self):
        result, calls, _ = self.resolve_fixture({'lzmhhh':mp3_sample(128), 'official_F000':flac_sample()})
        self.assertEqual(result['ext'], 'flac')
        self.assertEqual(result['raw_data']['available_bitrates'], [128000])
        self.assertGreater(calls.index('official_F000'), calls.index('mikus'))

    def test_low_mp3_only_is_explicit_quality_failure_and_restores_module(self):
        module, client, transport, calls = fixture('QQMusicClient', {'lzmhhh':mp3_sample(192)})
        original = module.SongFileType
        with patch.object(metadata, '_load_adapter', return_value=(module, client, Song)):
            with self.assertRaises(metadata.QualityUnavailableError) as caught:
                metadata.resolve(RECORD, '.')
        self.assertEqual(caught.exception.available_bitrates, [192000])
        self.assertEqual(caught.exception.error_kind, 'quality_unavailable')
        self.assertIs(module.requests, transport)
        self.assertIs(module.SongFileType, original)
        self.assertNotIn('official_AI00', calls)

    def test_enhanced_flac_never_becomes_ordinary_fallback(self):
        result, calls, _ = self.resolve_fixture({'vkeys_8':flac_sample(24,96000), 'official_F000':flac_sample()})
        self.assertEqual(result['raw_data']['requested_quality'], 'F000')
        self.assertTrue(any(attempt['outcome'] == 'enhanced_flac_rejected' for attempt in result['raw_data']['resolution_attempts']))

    def test_wrong_identifier_cannot_win(self):
        result, _, _ = self.resolve_fixture({'official_M800':mp3_sample(), 'lzmhhh':mp3_sample()}, mismatch='official_M800')
        self.assertEqual(result['raw_data']['resolution_provider'], 'lzmhhh')
        self.assertEqual(result['raw_data']['resolution_attempts'][0]['outcome'], 'id_mismatch')

    def test_netease_single_exhigh_for_each_provider_before_lossless(self):
        result, calls, _ = self.resolve_fixture({'official_lossless':flac_sample()}, 'NeteaseMusicClient')
        self.assertEqual(calls, ['official_exhigh','lzmhhh_exhigh','tmetu_exhigh','xuanluoge_exhigh','official_lossless'])
        self.assertEqual(result['raw_data']['resolution_phase'], 'ordinary_flac')

    def test_exhausted_budget_never_invokes_network(self):
        module, client, transport, calls = fixture('QQMusicClient', {})
        exhausted = metadata._Budget(total=-1, mp3=-1)
        with patch.object(metadata, '_load_adapter', return_value=(module,client,Song)), patch.object(metadata, '_Budget', return_value=exhausted):
            with self.assertRaises(metadata.QualityUnavailableError):
                metadata.resolve(RECORD, '.')
        self.assertEqual(calls, [])
        self.assertEqual(transport.responses, [])


if __name__ == '__main__':
    unittest.main()
