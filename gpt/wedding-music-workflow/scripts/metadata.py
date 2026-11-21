"""Metadata-only platform search. No audio URLs or third-party parsers during preflight."""
import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.request
import urllib.parse
import time


class SourceHTTPError(ValueError):
    """Sanitized HTTP failure; never expose request URLs or response bodies."""
    def __init__(self, status, retry_after=None):
        super().__init__('Music source returned an HTTP error')
        self.http_status = int(status)
        self.error_kind = 'rate_limited' if status == 429 else 'http_error'
        self.retry_after_seconds = None
        if retry_after:
            try:
                delay = float(retry_after)
            except (ValueError, TypeError):
                try:
                    when = parsedate_to_datetime(retry_after)
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    delay = (when-datetime.now(timezone.utc)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    delay = None
            if delay is not None and 0 <= delay < float('inf'):
                self.retry_after_seconds = delay


def request(url, payload, headers, timeout=15):
    req = urllib.request.Request(url, data=payload, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise SourceHTTPError(exc.code, exc.headers.get('Retry-After')) from None


def search(source, query, limit=5):
    headers = {'User-Agent': 'Mozilla/5.0', 'Content-Type': 'application/json'}
    if source == 'QQMusicClient':
        headers['Referer'] = 'https://y.qq.com/'
        from musicdl.modules.utils.qqutils import QQMusicClientUtils
        key = 'music.search.SearchCgiService.DoSearchForQQMusicMobile'
        payload = QQMusicClientUtils.buildrequestdata(
            params={'query':query,'search_type':0,'num_per_page':limit,'page_num':1,
                    'searchid':QQMusicClientUtils.randomsearchid(),'highlight':1,'grp':1},
            module='music.search.SearchCgiService',method='DoSearchForQQMusicMobile')
        result = request('https://u.y.qq.com/cgi-bin/musicu.fcg', json.dumps(payload).encode(), headers)
        block = result.get(key, {})
        if result.get('code', 0) != 0 or block.get('code', 0) != 0:
            raise ValueError('QQ search service rejected request')
        body = block.get('data', {}).get('body')
        if not isinstance(body, dict):
            raise ValueError('QQ search response schema changed')
        if 'item_song' in body:
            songs = body['item_song']
        elif 'song' in body:
            songs = body['song'].get('list', [])
        else:
            raise ValueError('QQ search response missing song list')
    elif source == 'NeteaseMusicClient':
        headers.update({'Referer': 'https://music.163.com/', 'Content-Type': 'application/x-www-form-urlencoded'})
        result = request('https://music.163.com/api/cloudsearch/pc',
                         urllib.parse.urlencode({'s': query, 'type': 1, 'limit': limit, 'offset': 0}).encode(), headers)
        if result.get('code') != 200:
            raise ValueError('NetEase search service rejected request')
        songs = result.get('result', {}).get('songs', [])
    else:
        raise ValueError('Metadata-only search supports QQ and NetEase')
    records = []
    for song in songs[:limit]:
        qq = source == 'QQMusicClient'
        identity = song.get('mid') or song.get('songmid') if qq else song.get('id')
        if not identity:
            continue
        duration = float(song.get('interval', 0) if qq else song.get('dt', 0)/1000)
        records.append({'source': source, 'identifier': str(identity),
            'song_name': song.get('title') or song.get('name') or song.get('songname'),
            'singers': [a['name'] for a in song.get('singer' if qq else 'ar', []) if a.get('name')],
            'album': song.get('album' if qq else 'al', {}).get('title' if qq else 'name'),
            'duration_s': duration, 'duration': f'{int(duration)//60}:{int(duration)%60:02}',
            'metadata_only': True, 'raw_data': {'search': song}})
    return records


class QualityUnavailableError(ValueError):
    error_kind = 'quality_unavailable'

    def __init__(self, available_bitrates=(), attempts=()):
        super().__init__('Selected ID has no verified 320 kbps MP3 or ordinary FLAC within the resolution budget')
        self.available_bitrates = sorted(set(available_bitrates))
        self.resolution_attempts = list(attempts)


class _Budget:
    """Soft per-attempt limits underneath the caller's 45-second hard limit."""
    def __init__(self, total=38, mp3=27, attempt=4):
        self.started = time.monotonic()
        self.deadline = self.started + total
        self.mp3_deadline = self.started + mp3
        self.attempt_seconds = attempt
        self.attempt_deadline = self.deadline

    def begin(self, mp3):
        end = min(self.deadline, self.mp3_deadline) if mp3 else self.deadline
        if end - time.monotonic() < 0.25:
            return False
        self.attempt_deadline = min(end, time.monotonic() + self.attempt_seconds)
        return True

    def remaining(self):
        remaining = min(self.deadline, self.attempt_deadline) - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Resolution attempt budget exhausted')
        return remaining

    def timeout(self):
        remaining = self.remaining()
        return (min(1.5, remaining), min(2, remaining))


class _BoundedRequests:
    """Local adapter module proxy; never changes shared requests or credentials."""
    def __init__(self, original, budget):
        self.original, self.budget = original, budget

    def __getattr__(self, name):
        method = getattr(self.original, name)
        if name not in {'get', 'post', 'head', 'request'}:
            return method
        def bounded(*args, **kwargs):
            kwargs['timeout'] = self.budget.timeout()
            return method(*args, **kwargs)
        return bounded


def _mp3_frame(data, offset):
    if offset + 4 > len(data):
        return None
    header = int.from_bytes(data[offset:offset+4], 'big')
    version, layer = (header >> 19) & 3, (header >> 17) & 3
    rate_index, sample_index = (header >> 12) & 15, (header >> 10) & 3
    if header >> 21 != 0x7ff or version == 1 or layer != 1 or rate_index in (0, 15) or sample_index == 3:
        return None
    rates = ([0,32,40,48,56,64,80,96,112,128,160,192,224,256,320]
             if version == 3 else [0,8,16,24,32,40,48,56,64,80,96,112,128,144,160])
    samplerate = [44100,48000,32000][sample_index] // (1 if version == 3 else 2 if version == 2 else 4)
    bitrate = rates[rate_index] * 1000
    length = ((144 if version == 3 else 72) * bitrate // samplerate) + ((header >> 9) & 1)
    return {'length':length, 'bitrate':bitrate, 'samplerate':samplerate,
            'version':version, 'channels':1 if ((header >> 6) & 3) == 3 else 2}


def inspect_audio_sample(data):
    """Inspect actual bytes, requiring three consecutive Layer III frames."""
    if data.startswith(b'fLaC') and len(data) >= 42 and data[4] & 127 == 0:
        size = int.from_bytes(data[5:8], 'big')
        if size == 34:
            packed = int.from_bytes(data[18:26], 'big')
            sample_rate, channels, bits = packed >> 44, ((packed >> 41) & 7)+1, ((packed >> 36) & 31)+1
            samples = packed & ((1 << 36)-1)
            if sample_rate > 0:
                return {'codec':'flac', 'bitrate':None, 'samplerate':sample_rate,
                        'channels':channels, 'bits_per_sample':bits, 'duration_s':samples/sample_rate}
    start = 0
    if data.startswith(b'ID3') and len(data) >= 10 and all(b < 128 for b in data[6:10]):
        size = sum(b << shift for b, shift in zip(data[6:10], (21,14,7,0)))
        start = 10 + size + (10 if data[5] & 16 else 0)
    for offset in range(start, max(start, len(data)-4)):
        first = _mp3_frame(data, offset)
        if not first:
            continue
        frames, cursor = [first], offset + first['length']
        for _ in range(2):
            frame = _mp3_frame(data, cursor)
            if not frame or (frame['version'], frame['samplerate']) != (first['version'], first['samplerate']):
                break
            frames.append(frame)
            cursor += frame['length']
        if len(frames) == 3:
            rates = [f['bitrate'] for f in frames]
            return {'codec':'mp3', 'bitrate':min(rates), 'bitrate_max':max(rates),
                    'samplerate':first['samplerate'], 'channels':first['channels']}
    return None


class _AudioProbe:
    MAX_BYTES = 32768
    TAG_FOLLOWUP_BYTES = 8192
    MAX_TAG_OFFSET = 16 * 1024 * 1024

    def __init__(self, transport, budget, headers=None, cookies=None):
        self.transport, self.budget = transport, budget
        self.headers, self.cookies = dict(headers or {}), dict(cookies or {})
        self.cache, self.observed = {}, []

    @staticmethod
    def _size(headers, status):
        # Content-Length on 206 is only the short sample, not the full file.
        total = str(headers.get('Content-Range', '')).rsplit('/', 1)[-1]
        if total.isdigit():
            return int(total)
        length = str(headers.get('Content-Length', ''))
        return int(length) if status == 200 and length.isdigit() else 0

    def _read(self, url, start, amount, overrides):
        kwargs = dict(overrides or {})
        headers = dict(self.headers)
        headers.update(kwargs.pop('headers', {}) or {})
        headers.update({'Range':f'bytes={start}-{start+amount-1}', 'Accept-Encoding':'identity'})
        kwargs.update(headers=headers, stream=True, timeout=self.budget.timeout())
        kwargs.setdefault('cookies', self.cookies)
        response = self.transport.get(url, **kwargs)
        try:
            response.raise_for_status()
            if response.status_code not in (200, 206):
                raise ValueError('Unsupported audio response')
            # A server ignoring a nonzero Range must not make us read a huge tag.
            if start and response.status_code != 206:
                raise ValueError('Audio server ignored bounded range')
            pieces, count = [], 0
            for chunk in response.iter_content(chunk_size=4096):
                self.budget.remaining()
                if not chunk:
                    continue
                piece = chunk[:amount-count]
                pieces.append(piece)
                count += len(piece)
                if count >= amount:
                    break
            return b''.join(pieces), self._size(response.headers, response.status_code), str(response.url), response.status_code
        finally:
            response.close()

    def test(self, url, request_overrides=None, renew_session=True):
        if url in self.cache:
            status = dict(self.cache[url])
            self.observed.append(status)
            return status
        if not isinstance(url, str) or urllib.parse.urlsplit(url).scheme not in {'http','https'}:
            raise ValueError('Unsupported audio URL')
        data, size, final_url, http_status = self._read(url, 0, self.MAX_BYTES, request_overrides)
        count, info = len(data), inspect_audio_sample(data)
        if info is None and data.startswith(b'ID3') and len(data) >= 10 and all(b < 128 for b in data[6:10]):
            offset = 10 + sum(b << shift for b, shift in zip(data[6:10], (21,14,7,0))) + (10 if data[5]&16 else 0)
            if len(data) <= offset <= self.MAX_TAG_OFFSET:
                extra, extra_size, final_url, http_status = self._read(url, offset, self.TAG_FOLLOWUP_BYTES, request_overrides)
                count += len(extra)
                info, size = inspect_audio_sample(extra), extra_size or size
        info = info or {}
        status = {'ok':bool(info), 'download_url':final_url, 'ext':info.get('codec','NULL'),
                  'file_size_bytes':size, 'file_size':f'{size/1048576:.2f} MB' if size else 'NULL',
                  'status_code':http_status, 'method':'bounded-range-byte-probe', 'reason':[],
                  'probe_sample_bytes':count, **{'probe_'+k:v for k,v in info.items()}}
        self.cache[url] = dict(status)
        self.observed.append(status)
        return status


def _load_adapter(source):
    from musicdl.modules import SongInfo
    if source == 'QQMusicClient':
        from musicdl.modules.sources import qq as module
        return module, module.QQMusicClient, SongInfo
    if source == 'NeteaseMusicClient':
        from musicdl.modules.sources import netease as module
        return module, module.NeteaseMusicClient, SongInfo
    raise ValueError('Selected source has no bounded MP3 resolver')


def _ordinary_flac(status):
    return (status.get('probe_codec') == 'flac' and 0 < status.get('probe_samplerate', 0) <= 48000
            and 0 < status.get('probe_bits_per_sample', 0) <= 16 and 0 < status.get('probe_channels', 0) <= 2)


def resolve(record, directory):
    """Try the bounded 320-MP3 routes before ordinary FLAC for the same ID.

    Only this worker's imported adapter references are temporarily changed.
    No account, cookie, installed package, or authentication policy is changed.
    """
    from types import SimpleNamespace
    budget = _Budget()
    module, cls, SongInfo = _load_adapter(record['source'])
    original_requests = module.requests
    qq = record['source'] == 'QQMusicClient'
    original_quality = module.SongFileType if qq else module.MUSIC_QUALITIES
    # BaseMusicClient otherwise recreates its AudioLinkTester on every get/post,
    # discarding the bounded byte probe installed below.
    client = cls(work_dir=directory, max_retries=1, disable_print=True, maintain_session=True)
    probe = _AudioProbe(original_requests, budget, getattr(client, 'default_download_headers', {}),
                        getattr(client, 'default_download_cookies', {}))
    client.audio_link_tester = probe
    module.requests = _BoundedRequests(original_requests, budget)
    for method in ('get', 'post'):
        original = getattr(client, method)
        def bounded(url, *a, _original=original, **kw):
            if 'lyric' in str(url).lower():
                raise ValueError('Lyrics disabled')
            kw['timeout'] = budget.timeout()
            return _original(url, *a, **kw)
        setattr(client, method, bounded)
    attempts, flac_candidates, available_bitrates = [], [], set()

    def quality(value):
        if qq:
            pair = next(q for q in original_quality.SORTED_QUALITIES.value if q[0] == value)
            module.SongFileType = SimpleNamespace(SORTED_QUALITIES=SimpleNamespace(value=[pair]))
        else:
            module.MUSIC_QUALITIES = [value]

    def official(value):
        quality(value)
        return client._parsewithofficialapiv1(search_result=record['raw_data']['search'],
                    song_info_flac=SongInfo(source=record['source']))

    def compatible(name, value):
        if not qq:
            quality(value)
        return getattr(client, name)(search_result=record['raw_data']['search'])

    def vkeys(value):
        response = module.requests.get('https://api.vkeys.cn/music/tencent/song/link',
                                      params={'mid':record['identifier'], 'quality':value})
        try:
            response.raise_for_status()
            url = response.json().get('data', {}).get('url')
        finally:
            response.close()
        if isinstance(url, str) and urllib.parse.urlsplit(url).scheme in {'http','https'}:
            probe.test(url)

    def finish(candidate, phase):
        status, provider, requested = candidate
        song = SongInfo(source=record['source'], identifier=str(record['identifier']),
            song_name=record['song_name'], singers=', '.join(record['singers']) if isinstance(record['singers'], list) else record['singers'],
            album=record.get('album') or '', duration_s=record['duration_s'], duration=record['duration'],
            ext=status['ext'], file_size_bytes=status['file_size_bytes'], file_size=status['file_size'],
            download_url=status['download_url'], download_url_status=status,
            raw_data={'search':record['raw_data']['search'],
                'resolution_strategy':'mp3-320-selected-id' if phase == 'mp3_320' else 'ordinary-flac-selected-id',
                'resolution_phase':phase, 'resolution_provider':provider, 'requested_quality':requested,
                'resolution_attempts':attempts, 'available_bitrates':sorted(available_bitrates),
                **{key:value for key,value in status.items() if key.startswith('probe_')}})
        return song.todict()

    def attempt(provider, requested, callback, mp3):
        if not budget.begin(mp3):
            attempts.append({'provider':provider, 'quality':requested, 'outcome':'budget_exhausted'})
            return None
        probe.observed = []
        started, outcome, error_type, http_status = time.monotonic(), 'unavailable', None, None
        try:
            result = callback()
            if result is not None and getattr(result, 'identifier', None) is not None and str(result.identifier) != str(record['identifier']):
                probe.observed = []
                outcome = 'id_mismatch'
        except Exception as exc:
            outcome = 'timeout' if isinstance(exc, TimeoutError) else 'request_failed'
            error_type = type(exc).__name__
            http_status = getattr(getattr(exc, 'response', None), 'status_code', None)
        winner = None
        for status in probe.observed:
            if not status.get('ok'):
                continue
            codec, bitrate = status.get('probe_codec'), status.get('probe_bitrate')
            if codec == 'mp3' and isinstance(bitrate, int):
                available_bitrates.add(bitrate)
                if bitrate == 320000 and status.get('probe_bitrate_max') == 320000:
                    winner, outcome = (status, provider, requested), 'mp3_320'
                    break
                outcome = 'lower_bitrate_mp3'
            elif _ordinary_flac(status):
                flac_candidates.append((status, provider, requested))
                outcome = 'ordinary_flac_cached'
            elif codec == 'flac':
                outcome = 'enhanced_flac_rejected'
        attempts.append({'provider':provider, 'quality':requested, 'outcome':outcome,
                         'elapsed_seconds':round(time.monotonic()-started, 3),
                         **({'error_type':error_type} if error_type else {}),
                         **({'http_status':http_status} if isinstance(http_status,int) else {})})
        return winner

    try:
        # The upstream legacy dispatchers mix master/lossless requests ahead of
        # MP3. Use a small explicit route list, retaining their selected-ID calls.
        mp3_routes = [('official', 'M800' if qq else 'exhigh', lambda:official('M800' if qq else 'exhigh'))]
        if qq:
            mp3_routes += [('vkeys', 8, lambda:vkeys(8)),
                ('lzmhhh', 'provider-default', lambda:compatible('_parsewithlzmhhhapi', None)),
                ('xunhuisi', 'provider-default', lambda:compatible('_parsewithxunhuisiapi', None)),
                ('mikus', 'provider-default', lambda:compatible('_parsewithmikusapi', None))]
        else:
            mp3_routes += [('lzmhhh', 'exhigh', lambda:compatible('_parsewithlzmhhhapi', 'exhigh')),
                ('tmetu', 'exhigh', lambda:compatible('_parsewithtmetuapi', 'exhigh')),
                ('xuanluoge', 'exhigh', lambda:compatible('_parsewithxuanluogeapi', 'exhigh'))]
        for provider, requested, callback in mp3_routes:
            if winner := attempt(provider, requested, callback, True):
                return finish(winner, 'mp3_320')
        if flac_candidates:
            return finish(min(flac_candidates, key=lambda c:c[0]['file_size_bytes'] or float('inf')), 'ordinary_flac')
        flac_routes = [('official', 'F000' if qq else 'lossless', lambda:official('F000' if qq else 'lossless'))]
        if qq:
            flac_routes += [('vkeys', 10, lambda:vkeys(10)),
                ('cy', 'lossless', lambda:compatible('_parsewithcyapi', None))]
        else:
            flac_routes += [('tmetu', 'lossless', lambda:compatible('_parsewithtmetuapi', 'lossless')),
                ('xuanluoge', 'lossless', lambda:compatible('_parsewithxuanluogeapi', 'lossless'))]
        for provider, requested, callback in flac_routes:
            if winner := attempt(provider, requested, callback, False):
                return finish(winner, 'mp3_320')
            if flac_candidates:
                return finish(flac_candidates[0], 'ordinary_flac')
        raise QualityUnavailableError(available_bitrates, attempts)
    finally:
        module.requests = original_requests
        if qq:
            module.SongFileType = original_quality
        else:
            module.MUSIC_QUALITIES = original_quality
