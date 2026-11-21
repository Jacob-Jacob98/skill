"""Parallel metadata preflight with fresh sessions and incremental review files.

Concurrency and cooldowns belong only to this invocation, not the whole machine
or other processes. Already running requests are not cancelled after HTTP 429.
"""
import argparse
import concurrent.futures
import contextlib
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid

import music
import pipeline


PRIORITY = ('QQMusicClient', 'NeteaseMusicClient')


@contextlib.contextmanager
def fresh_work(work, batch_id):
    """Claim an empty directory exclusively; never reuse an approval workspace."""
    if work.is_symlink() or (hasattr(work, 'is_junction') and work.is_junction()):
        raise ValueError('Preflight work must not be a link or junction')
    work.mkdir(parents=True, exist_ok=True)
    lock = work / '.preflight.lock'
    try:
        stream = lock.open('x', encoding='utf-8')
    except FileExistsError:
        raise ValueError('Preflight work is already claimed; use a new empty directory') from None
    try:
        with stream:
            json.dump({'batch_id': batch_id, 'pid': os.getpid()}, stream)
        if any(path != lock for path in work.iterdir()):
            raise ValueError('Preflight work is not empty; use a new empty directory to preserve existing reviews')
        yield
    finally:
        lock.unlink(missing_ok=True)


class BatchState:
    """Thread-safe accounting and nonblocking, per-batch source cooldowns."""

    def __init__(self):
        self.lock = threading.Lock()
        self.active_tracks = self.active_requests = 0
        self.peak_tracks = self.peak_requests = 0
        self.active_sources = {}
        self.peak_sources = {}
        self.blocked_until = {}

    def enter_track(self):
        with self.lock:
            self.active_tracks += 1
            self.peak_tracks = max(self.peak_tracks, self.active_tracks)

    def leave_track(self):
        with self.lock:
            self.active_tracks -= 1

    def begin_request(self, source):
        with self.lock:
            remaining = self.blocked_until.get(source, 0) - time.perf_counter()
            if remaining > 0:
                return remaining
            self.active_requests += 1
            self.peak_requests = max(self.peak_requests, self.active_requests)
            self.active_sources[source] = self.active_sources.get(source, 0) + 1
            self.peak_sources[source] = max(self.peak_sources.get(source, 0), self.active_sources[source])
            return 0

    def finish_request(self, source, result):
        with self.lock:
            self.active_requests -= 1
            self.active_sources[source] -= 1
            if result.get('http_status') == 429 or result.get('error_kind') == 'rate_limited':
                delay = result.get('retry_after_seconds')
                if (isinstance(delay, bool) or not isinstance(delay, (int, float))
                        or not math.isfinite(delay) or delay < 0):
                    delay = 10
                self.blocked_until[source] = max(self.blocked_until.get(source, 0), time.perf_counter() + delay)

    def metrics(self):
        with self.lock:
            return {'peak_concurrent_tracks': self.peak_tracks,
                    'peak_concurrent_requests': self.peak_requests,
                    'source_peak_concurrency': {music.LABELS[s]: n for s, n in self.peak_sources.items()}}


def pending_row(track, session):
    row = dict(track)
    # A new candidate list invalidates every prior version choice and approval.
    for key in ('candidate_id', 'source_file', 'session', 'confirmed', 'confirmed_at', 'confirmed_by'):
        row.pop(key, None)
    row.update(confirmed=False, session=str(session))
    return row


def track_result(track, session, query, records, statuses, elapsed, error=None):
    candidates = [{'id': i + 1, 'query': query, 'record': r, 'confirmed': False}
                  for i, r in enumerate(records)]
    status = ('failed' if error else 'needs_review' if records else
              'needs_retry' if any(s['status'] in ('failed', 'deferred') for s in statuses) else 'not_found')
    review = {'index': track['index'], 'query': query,
              'status': status,
              'session': str(session), 'sources': statuses,
              'elapsed_seconds': round(elapsed, 3),
              'candidates': [music.public_record(r, i + 1, query) for i, r in enumerate(records)]}
    if error:
        review.update(error=error, error_kind='track_error')
    return {'pending': pending_row(track, session), 'review': review,
            'session': {'schema': 1, 'created': time.time(), 'candidates': candidates, 'sources': statuses}}


def search_track(track, session, args, sources, deps, state):
    started = time.perf_counter()
    state.enter_track()
    query, records, statuses = '', [], []
    try:
        artists = track.get('artists', [])
        query = track.get('query') or ' '.join([
            track['title'], ' '.join(artists) if isinstance(artists, list) else artists,
            track.get('version', '')]).strip()
        for source in sources:
            remaining = state.begin_request(source)
            if remaining:
                statuses.append({'source': music.LABELS[source], 'elapsed_seconds': 0,
                                 'status': 'deferred', 'error_kind': 'rate_limited',
                                 'retry_after_seconds': round(remaining, 3)})
                continue
            tick = time.perf_counter()
            result = {}
            try:
                folder = args.work / 'search-jobs' / f"{track['index']}-{uuid.uuid4().hex}"
                result = music.run_job({'action': 'metadata', 'source': source,
                                       'query': query, 'limit': args.limit},
                                      deps, args.timeout, folder)
                if not isinstance(result, dict):
                    raise TypeError('Invalid source result')
                records = (result.get('records') or []) if result.get('ok') else []
                if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
                    raise TypeError('Invalid metadata records')
            except Exception as exc:
                records = []
                result = {'ok': False, 'error_kind': 'source_error',
                          'error': type(exc).__name__ + ': metadata request failed'}
            finally:
                state.finish_request(source, result)
            status = {'source': music.LABELS[source],
                      'elapsed_seconds': round(time.perf_counter() - tick, 3),
                      'status': 'ok' if records else 'empty' if result.get('ok') else 'failed',
                      'candidate_count': len(records)}
            for field in ('error', 'error_kind', 'http_status', 'retry_after_seconds'):
                if field in result:
                    status[field] = result[field]
            statuses.append(status)
            if records:
                break
        return track_result(track, session, query, records, statuses, time.perf_counter() - started)
    except Exception as exc:
        return track_result(track, session, query, [], statuses, time.perf_counter() - started,
                            type(exc).__name__ + ': track preflight failed')
    finally:
        state.leave_track()


def run(args):
    args.work, args.manifest = Path(args.work).absolute(), Path(args.manifest).resolve()
    args.workers = getattr(args, 'workers', 4)
    args.timeout = getattr(args, 'timeout', 20)
    if isinstance(args.workers, bool) or not isinstance(args.workers, int) or not 1 <= args.workers <= 15:
        raise ValueError('workers must be between 1 and 15')
    if isinstance(args.timeout, bool) or not isinstance(args.timeout, int) or not 1 <= args.timeout <= 120:
        raise ValueError('timeout must be between 1 and 120 seconds')
    data = pipeline.manifest(args.manifest)
    requested = music.resolve_sources(args.sources)
    if any(source not in PRIORITY for source in requested):
        raise ValueError('Preflight only supports QQ and Netease metadata sources')
    sources = [source for source in PRIORITY if source in requested]
    deps = music.dependencies(args.deps)
    batch_id = uuid.uuid4().hex
    with fresh_work(args.work, batch_id):
        started = time.perf_counter()
        state, finished = BatchState(), {}
        sessions = [args.work / f"session-{t['index']}-{uuid.uuid4().hex}.json" for t in data['tracks']]
        saved_snapshots = {}

        def save_owned(path, payload):
            # Preserve edits made by a reviewer while this batch is still running.
            generated = path
            suffix = 0
            while generated.exists() and generated.read_text(encoding='utf-8') != saved_snapshots.get(generated):
                number = '' if suffix == 0 else '-' + str(suffix)
                generated = path.with_name(path.stem + '.generated-' + batch_id + number + path.suffix)
                suffix += 1
            pipeline.save(generated, payload)
            saved_snapshots[generated] = json.dumps(payload, ensure_ascii=False, indent=2)
            return str(generated)

        def snapshot(complete=False):
            positions = sorted(finished)
            reviews = [finished[i]['review'] for i in positions]
            rows = [finished[i]['pending'] for i in positions]
            wall = round(time.perf_counter() - started, 3)
            report = {'schema': 2, 'batch_id': batch_id, 'complete': complete,
                      'completed': len(finished), 'total': len(data['tracks']),
                      'workers': args.workers, 'source_timeout_seconds': args.timeout,
                      'concurrency_scope': 'this_preflight_batch_only',
                      'concurrency_note': 'No machine-wide or cross-process limit; in-flight requests are not cancelled on 429.',
                      'rate_limit_policy': 'Skip sources in this batch cooldown; no waiting or automatic retries.',
                      'elapsed_seconds': wall, 'wall_seconds': wall, **state.metrics()}
            report['pending_manifest'] = save_owned(args.work / 'manifest.pending.json',
                {'batch_id': batch_id, 'complete': complete, 'tracks': rows})
            report['review_file'] = save_owned(args.work / 'review.json', dict(report, tracks=reviews))
            save_owned(args.work / 'preflight-report.json', dict(report, tracks=reviews))

        snapshot()
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(search_track, track, sessions[i], args, sources, deps, state): i
                       for i, track in enumerate(data['tracks'])}
            for future in concurrent.futures.as_completed(futures):
                position = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = track_result(data['tracks'][position], sessions[position], '', [], [], 0,
                                          type(exc).__name__ + ': track worker failed')
                music.write_json(sessions[position], result['session'])
                finished[position] = result
                snapshot()
                row = result['review']
                print(json.dumps({'index': row['index'], 'candidate_count': len(row['candidates']),
                                  'status': row['status']}, ensure_ascii=False), flush=True)
        snapshot(complete=True)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True, help='New or empty directory; existing reviews are never reused')
    parser.add_argument('--sources', default='QQ,网易云', help='QQ and/or 网易云; QQ always takes priority')
    parser.add_argument('--workers', type=int, default=4, choices=range(1, 16), help='Concurrent tracks in this batch only')
    parser.add_argument('--limit', type=int, default=5, choices=range(1, 21))
    parser.add_argument('--timeout', type=int, default=20, choices=range(1, 121), help='Hard timeout in seconds for each source attempt')
    parser.add_argument('--deps')
    args = parser.parse_args(argv)
    try:
        return run(args)
    except Exception as exc:
        print(json.dumps({'error': str(exc), 'type': type(exc).__name__}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
