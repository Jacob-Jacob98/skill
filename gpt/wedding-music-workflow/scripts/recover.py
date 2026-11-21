"""One sequential, bounded recovery attempt for confirmed QQ tracks that failed this batch.

Recovered audio is an intermediate source: run pipeline again for strict decoding,
encoding and publication. No credentials or audio URLs are written to reports.
"""
from __future__ import annotations
import argparse
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import time

import requests
import metadata
import music
import pipeline

MAX_BYTES = 80 * 1024 * 1024


class RateLimited(Exception):
    def __init__(self, retry_after=None):
        super().__init__('rate_limited')
        self.retry_after = metadata.SourceHTTPError(429, retry_after).retry_after_seconds


class Transport:
    def __init__(self, backend=requests):
        self.backend = backend

    def checked(self, response):
        if response.status_code == 429:
            delay = response.headers.get('Retry-After')
            response.close()
            raise RateLimited(delay)
        return response

    def get(self, *args, **kwargs):
        return self.checked(self.backend.get(*args, **kwargs))

    def post(self, *args, **kwargs):
        return self.checked(self.backend.post(*args, **kwargs))


class Budget:
    def __init__(self):
        self.end = time.monotonic() + 35

    def remaining(self):
        value = self.end - time.monotonic()
        if value <= 0:
            raise TimeoutError('probe_budget')
        return value

    def timeout(self):
        return min(5, self.remaining()), min(15, self.remaining())


def quality_allowed(info, allow_low):
    if info['source_format'] == 'mp3':
        return info['source_bitrate'] == 320000 or (allow_low and 0 < info['source_bitrate'] < 320000)
    return (info['source_format'] == 'flac' and 0 < info['source_sample_rate'] <= 48000
            and 0 < info['source_bits_per_sample'] <= 16 and 0 < info['source_channels'] <= 2)


def attempt(record, folder, allow_low=False, transport=None):
    transport = transport or Transport()
    response = transport.post('https://music.lzmhhh.com/api/music/url',
        data={'id': record['identifier'], 'type': 'qq'},
        headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://music.lzmhhh.com/',
                 'Origin': 'https://music.lzmhhh.com'}, timeout=(5, 15))
    try:
        response.raise_for_status()
        url = response.json().get('data')
    finally:
        response.close()
    probe = metadata._AudioProbe(transport, Budget())
    status = probe.test(url)
    if not status.get('ok'):
        return {'status': 'failed', 'error_kind': 'unrecognized_audio'}
    bitrate = status.get('probe_bitrate')
    allowed = metadata._ordinary_flac(status) or (status.get('probe_codec') == 'mp3'
        and (bitrate == 320000 or (allow_low and isinstance(bitrate, (int, float)) and 0 < bitrate < 320000)))
    if not allowed:
        return {'status': 'failed', 'error_kind': 'quality_unavailable',
                'available_bitrates': [bitrate] if bitrate else []}
    if status.get('file_size_bytes', 0) > MAX_BYTES:
        raise ValueError('size_limit')
    target = folder / ('source.' + status['ext'])
    part = folder / ('source.' + status['ext'] + '.partial')
    started = time.monotonic()
    try:
        with transport.get(status['download_url'], stream=True, timeout=(5, 15)) as response:
            response.raise_for_status()
            size = 0
            with part.open('xb') as stream:
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_BYTES or time.monotonic() - started > 150:
                        raise TimeoutError('transfer_budget')
                    stream.write(chunk)
        info = music.inspect_download(part, record['duration_s'])
        if not quality_allowed(info, allow_low):
            return {'status': 'failed', 'error_kind': 'quality_changed', 'quality': info}
        part.rename(target)
        return {'status': 'downloaded', 'path': str(target.resolve()), 'quality': info,
                'source_sha256': pipeline.digest(target), 'fresh_network': True,
                'validation': 'metadata-and-duration; pipeline strict decode still required',
                'download_seconds': round(time.monotonic()-started, 3)}
    finally:
        part.unlink(missing_ok=True)


def parse_ids(value):
    try:
        values = [int(v.strip()) for v in value.split(',') if v.strip()]
    except ValueError:
        raise ValueError('IDs must be positive integers') from None
    if not values or any(v <= 0 for v in values) or len(values) != len(set(values)):
        raise ValueError('IDs must be unique positive integers')
    return values


def prepare(manifest_path, work, indices, allow_low):
    result = pipeline.manifest(manifest_path)
    report = pipeline.load(work / 'report.json')
    tracks = {t['index']: t for t in result['tracks']}
    rows = {r['index']: r for r in report['results']}
    if not set(indices) <= tracks.keys() or not set(allow_low) <= set(indices):
        raise ValueError('Unknown or unselected track ID')
    selections = []
    for index in indices:
        track = tracks[index]
        row = rows.get(index, {})
        if track.get('confirmed') is not True or track.get('source_file'):
            raise ValueError('Recovery requires an explicitly confirmed online candidate')
        if row.get('status') != 'failed' or row.get('job_digest') != pipeline.job_digest(track):
            raise ValueError('Failed batch evidence does not match this manifest')
        session_path = pipeline.absolute(track['session'], manifest_path.parent)
        session = pipeline.load(session_path)
        age = time.time() - float(session.get('created', 0))
        if not 0 <= age < 86400:
            raise ValueError('Session expired; search and confirm the exact version again')
        matches = [c for c in session['candidates'] if c['id'] == track['candidate_id']]
        if len(matches) != 1:
            raise ValueError('Candidate ID does not exist uniquely in this session')
        record = matches[0]['record']
        if record.get('source') != 'QQMusicClient' or record.get('metadata_only') is not True:
            raise ValueError('This recovery path supports QQ metadata candidates only')
        if not record.get('identifier') or not isinstance(record.get('duration_s'), (int, float)) or record['duration_s'] <= 0:
            raise ValueError('Candidate has incomplete identity or duration')
        selections.append((track, record))
    # Relative paths must still resolve after the resulting manifest is moved.
    updated = copy.deepcopy(result)
    for track in updated['tracks']:
        for field in ('session', 'source_file'):
            if track.get(field):
                track[field] = str(pipeline.absolute(track[field], manifest_path.parent))
    return selections, updated


def recover(args, attempt_fn=attempt):
    with pipeline.batch_lock(args.work.resolve()):
        return recover_batch(args, attempt_fn)


def recover_batch(args, attempt_fn=attempt):
    manifest_path, work, out = args.manifest.resolve(), args.work.resolve(), args.out.resolve()
    indices = parse_ids(args.ids)
    allow_low = parse_ids(args.allow_low_bitrate_ids) if args.allow_low_bitrate_ids else []
    if out.exists() or out == work or out.is_relative_to(work) or work.is_relative_to(out):
        raise ValueError('Recovery output must be a new directory separate from the batch')
    selections, updated = prepare(manifest_path, work, indices, allow_low)
    ledger = work / '.recovery-attempts'
    ledger.mkdir(exist_ok=True)
    markers = [(ledger / (pipeline.job_digest(track) + '.json')) for track, _ in selections]
    if any(p.exists() for p in markers):
        raise ValueError('A recovery attempt was already recorded for a selected track')
    results = []
    out.mkdir(parents=True, exist_ok=False)
    for (track, record), marker in zip(selections, markers):
        index = track['index']
        # Persist before requesting; interrupted runs may not silently retry.
        with marker.open('x', encoding='utf-8') as stream:
            json.dump({'index': index, 'started': time.time(), 'out': str(out)}, stream)
        folder = out / str(index)
        folder.mkdir()
        entry = {'index': index, 'provider': 'lzmhhh', 'low_bitrate_authorized': index in allow_low}
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                entry.update(attempt_fn(record, folder, index in allow_low))
        except RateLimited as exc:
            entry.update(status='rate_limited', http_status=429,
                         retry_after_seconds=exc.retry_after)
        except Exception as exc:
            entry.update(status='failed', error_kind=type(exc).__name__)
        results.append(entry)
        if entry['status'] == 'downloaded':
            target = next(t for t in updated['tracks'] if t['index'] == index)
            target.pop('session', None)
            target.pop('candidate_id', None)
            target['source_file'] = entry['path']
        pipeline.save(out / 'recovery-report.json', {'results': results, 'strict_pipeline_required': True})
        pipeline.save(out / 'recover-manifest.json', updated)
        print(json.dumps(entry, ensure_ascii=False), flush=True)
        if entry['status'] == 'rate_limited':
            break
    return 0 if len(results) == len(indices) and all(r['status']=='downloaded' for r in results) else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('manifest', 'work', 'out'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--ids', required=True, help='Failed playlist indices, comma-separated')
    parser.add_argument('--allow-low-bitrate-ids', default='', help='Only indices the user explicitly approved for lower source quality')
    args = parser.parse_args()
    return recover(args)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({'ok': False, 'error': type(exc).__name__,
                          'detail': str(exc) if isinstance(exc, ValueError) else 'Check local paths and input files'}), file=sys.stderr)
        raise SystemExit(2)
