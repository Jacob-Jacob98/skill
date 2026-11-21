"""Persistent, numbered music selection using musicdl; no GUI dependency."""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

SOURCES = {
    '酷狗': 'KugouMusicClient', '酷我': 'KuwoMusicClient',
    '网易云': 'NeteaseMusicClient', 'QQ': 'QQMusicClient', '咪咕': 'MiguMusicClient',
    '汽水': 'SodaMusicClient', '千千': 'QianqianMusicClient', '5sing': 'FiveSingMusicClient',
    'Jamendo': 'JamendoMusicClient', 'SoundCloud': 'SoundCloudMusicClient',
    'StreetVoice': 'StreetVoiceMusicClient', 'Deezer': 'DeezerMusicClient',
    'Joox': 'JooxMusicClient', 'Qobuz': 'QobuzMusicClient', 'Spotify': 'SpotifyMusicClient',
    'AppleMusic': 'AppleMusicClient', 'TIDAL': 'TIDALMusicClient',
    '维基共享': 'WikimediaCommonsMusicClient',
}
LABELS = {v: k for k, v in SOURCES.items()}
EXTS = {'.mp3', '.flac', '.m4a', '.aac', '.wav', '.ogg', '.opus', '.aiff', '.alac', '.ape', '.wv'}


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def dependencies(explicit=None):
    choices = [explicit, os.environ.get('MUSIC_PICK_DEPS'), Path(__file__).resolve().parents[1] / '.deps']
    for item in choices:
        if item and (Path(item) / 'musicdl').is_dir():
            return str(Path(item).resolve())
    return None


def pack(value):
    if isinstance(value, bytes):
        return {'__music_bytes__': base64.b64encode(value).decode('ascii')}
    if isinstance(value, dict):
        return {str(k): pack(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [pack(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError('该来源返回不可缓存的流对象，请换一个来源搜索')


def unpack(value):
    if isinstance(value, dict):
        if set(value) == {'__music_bytes__'}:
            return base64.b64decode(value['__music_bytes__'])
        return {k: unpack(v) for k, v in value.items()}
    if isinstance(value, list):
        return [unpack(v) for v in value]
    return value


def flatten(results):
    if isinstance(results, dict):
        return [song for group in results.values() for song in group]
    return results or []


def worker(job_path):
    job = read_json(job_path)
    result = {'ok': False, 'error': '运行失败'}
    # Upstream logs and raw URLs/cookies must not enter the conversation.
    with open(os.devnull, 'w', encoding='utf-8') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            if job['action'] == 'metadata':
                from metadata import search as metadata_search
                records = metadata_search(job['source'], job['query'], job.get('limit', 5))
                write_json(job['result'], {'ok': True, 'records': records})
                return
            if job['action'] == 'resolve':
                from metadata import resolve
                record = resolve(unpack(job['record']), job['temp'])
                write_json(job['result'], {'ok':True,'record':pack(record)})
                return
            from musicdl import musicdl
            from musicdl.modules import SongInfo
            from musicdl.modules.utils.songinfoutils import SongInfoUtils
            from functools import partial
            SongInfoUtils.supplsonginfothensavelyricsthenwritetags = staticmethod(partial(SongInfoUtils.supplsonginfothensavelyricsthenwritetags, auto_save_lyrics_then_write_tags=False))
            source = job['source']
            cfg = {source: {'work_dir': job['temp'], 'search_size_per_source': job.get('limit', 5),
                            'max_retries': 1, 'disable_print': True}}
            client = musicdl.MusicClient(music_sources=[source], init_music_clients_cfg=cfg,
                                        clients_threadings={source: 1})
            if job['action'] in {'search', 'playlist'}:
                songs = flatten(client.search(keyword=job['query']) if job['action'] == 'search'
                                else client.parseplaylist(job['query']))
                records, unsupported = [], 0
                for song in songs:
                    try:
                        data = song.todict() if hasattr(song, 'todict') else dict(song)
                        if data.get('episodes'):
                            unsupported += 1
                            continue
                        data['source'] = source
                        records.append(pack(data))
                    except (ValueError, TypeError):
                        unsupported += 1
                result = {'ok': True, 'records': records, 'unsupported': unsupported}
            else:
                data = unpack(job['record'])
                if data.get('metadata_only') or (source in {'QQMusicClient','NeteaseMusicClient'}
                        and data.get('raw_data', {}).get('search')):
                    tick = time.perf_counter()
                    resolved = run_job({'action':'resolve','source':source,'record':job['record']},
                                       None, 45, Path(job['temp'])/'resolution')
                    if not resolved.get('ok'):
                        # Preserve sanitized quality/rate-limit diagnostics across
                        # the resolver subprocess instead of flattening every failure.
                        resolved['resolve_seconds'] = round(time.perf_counter()-tick,3)
                        write_json(job['result'], resolved)
                        return
                    data = unpack(resolved['record'])
                    resolve_seconds = round(time.perf_counter()-tick,3)
                data.update(work_dir=job['temp'], _save_path=None)
                song = SongInfo.fromdict(data)
                client.requests_overrides[source]['timeout'] = (5, 20)
                download_started = time.perf_counter()
                downloaded = client.download(song_infos=[song])
                paths = []
                root = Path(job['temp']).resolve()
                for saved in flatten(downloaded):
                    # _save_path is stable; save_path may generate a NEW name after a file exists.
                    candidate = getattr(saved, '_save_path', None) or getattr(saved, 'save_path', None)
                    if candidate:
                        path = Path(candidate).resolve()
                        if path.is_relative_to(root) and path.is_file() and path.suffix.lower() in EXTS:
                            paths.append(str(path))
                result = {'ok': bool(paths), 'paths': paths, 'download_seconds':round(time.perf_counter()-download_started,3), 'resolve_seconds':locals().get('resolve_seconds',0),
                          'resolution_strategy':data.get('raw_data',{}).get('resolution_strategy','legacy-session'),
                          'error': '来源未返回成功保存的音频文件'}
                for field in ('resolution_provider', 'requested_quality', 'resolution_phase', 'resolution_attempts'):
                    if field in data.get('raw_data', {}):
                        result[field] = data['raw_data'][field]
        except Exception as exc:
            # Exception messages can contain tokens and authenticated URLs.
            result = {'ok': False, 'error': type(exc).__name__ + '：来源不可用、依赖不兼容或请求失败'}
            result['error_kind'] = getattr(exc, 'error_kind',
                'timeout' if isinstance(exc, TimeoutError) else 'source_error')
            for field in ('http_status', 'retry_after_seconds'):
                value = getattr(exc, field, None)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    result[field] = value
            bitrates = getattr(exc, 'available_bitrates', None)
            if isinstance(bitrates, (list, tuple)):
                result['available_bitrates'] = [int(x) for x in bitrates
                    if isinstance(x, (int, float)) and not isinstance(x, bool) and 0 < x <= 320000]
            attempts = getattr(exc, 'resolution_attempts', None)
            if isinstance(attempts, list):
                result['resolution_attempts'] = attempts
    write_json(job['result'], result)


def run_job(job, deps, timeout, root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    job = dict(job, result=str(root / 'result.json'), temp=str(root / 'audio'))
    Path(job['temp']).mkdir(exist_ok=True)
    write_json(root / 'job.json', job)
    env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    if deps:
        env['PYTHONPATH'] = deps + os.pathsep + env.get('PYTHONPATH', '')
    try:
        proc = subprocess.run([sys.executable, '-X', 'utf8', str(Path(__file__).resolve()),
                               '_worker', str(root / 'job.json')],
                              env=env, cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=timeout)
        if proc.returncode or not Path(job['result']).is_file():
            return {'ok': False, 'error': '后台任务失败；请运行 doctor 检查依赖'}
        return read_json(job['result'])
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error_kind': 'timeout', 'error': f'超过 {timeout} 秒，已停止该来源'}


def resolve_sources(text):
    result = []
    for token in text.split(','):
        token = token.strip()
        source = next((v for k, v in SOURCES.items() if k.casefold() == token.casefold() or v.casefold() == token.casefold()), None)
        if not source:
            raise ValueError(f'未知来源：{token}；使用 sources 查看可选项')
        if source not in result:
            result.append(source)
    return result


def public_record(record, number, query):
    return {'id': number, 'query': query, 'song': record.get('song_name'),
            'singers': record.get('singers'), 'album': record.get('album'),
            'format': record.get('ext'), 'size': record.get('file_size'),
            'duration': record.get('duration'), 'bitrate': record.get('bitrate'),
            'source': LABELS.get(record.get('source'), record.get('source'))}


def search(args):
    cache = args.session.resolve()
    if cache.exists():
        raise ValueError('会话文件已存在；请使用新的 session 文件名，避免旧编号失效')
    cache.parent.mkdir(parents=True, exist_ok=True)
    sources = resolve_sources(args.sources)
    queries = args.query or [args.url]
    jobs = [(source, query) for query in queries for source in sources]
    candidates, statuses, seen = [], [], set()
    deps = dependencies(args.deps)
    with tempfile.TemporaryDirectory(prefix='music-search-', dir=cache.parent) as temp:
        def task(index_pair):
            i, (source, query) = index_pair
            started = time.perf_counter()
            result = run_job({'action': 'metadata' if getattr(args,'metadata_only',False) else args.command, 'source': source, 'query': query, 'limit': args.limit},
                           deps, args.timeout, Path(temp) / str(i))
            result['elapsed_seconds'] = round(time.perf_counter()-started,3)
            return result
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            results = list(pool.map(task, enumerate(jobs)))
        for (source, query), result in zip(jobs, results):
            status = {'source': LABELS[source], 'query': query, 'elapsed_seconds': result['elapsed_seconds'], 'count': len(result.get('records', [])),
                      'status': ('ok' if result.get('records') else 'empty') if result['ok'] else 'failed'}
            if not result['ok']:
                status['error'] = result.get('error')
            if result.get('unsupported'):
                status['unsupported_streams'] = result['unsupported']
            statuses.append(status)
            for record in result.get('records', []):
                key = record_key(record)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append({'id': len(candidates) + 1, 'query': query, 'record': record})
    data = {'schema': 1, 'created': time.time(), 'candidates': candidates, 'sources': statuses}
    write_json(cache, data)
    emit({'session': str(cache), 'candidates': [public_record(c['record'], c['id'], c['query']) for c in candidates],
          'sources': statuses})
    return 0 if candidates else 2


def record_key(record):
    identity = record.get('identifier')
    if identity is None or identity == '':
        identity = [record.get(k) for k in ('song_name', 'singers', 'album', 'duration', 'ext')]
    return hashlib.sha256(json.dumps([record.get('source'), identity], ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def audio_valid(path):
    if path.stat().st_size < 128:
        return False
    with path.open('rb') as stream:
        head = stream.read(64)
    return (head.startswith((b'ID3', b'fLaC', b'RIFF', b'OggS', b'FORM', b'MAC ', b'wvpk'))
            or head[4:8] == b'ftyp' or (head[0] == 255 and head[1] & 224 == 224))


def inspect_download(path, expected_duration=None):
    """Read the actual downloaded stream, never infer its quality from a URL/suffix."""
    from mutagen import File
    from mutagen.mp3 import MP3
    from mutagen.flac import FLAC
    with Path(path).open('rb') as stream:
        magic = stream.read(4)
    # Mutagen's generic selector also scores the filename; prefer an explicit
    # container signature when present, so a FLAC mislabeled .mp3 stays FLAC.
    audio = FLAC(path) if magic == b'fLaC' else File(path)
    if audio is None or not getattr(audio.info, 'length', 0) > 0:
        raise ValueError('Downloaded audio has no readable duration')
    if expected_duration is not None:
        expected = float(expected_duration)
        if expected <= 0 or abs(audio.info.length-expected) > 2:
            raise ValueError('Downloaded duration differs from selected version; possible preview or wrong audio')
    codec = type(audio).__name__.lower()
    bitrate = int(getattr(audio.info, 'bitrate', 0) or 0)
    is_mp3_320 = isinstance(audio, MP3) and abs(bitrate-320000) <= 100
    if is_mp3_320:
        fallback = None
    elif isinstance(audio, MP3):
        fallback = 'mp3_other_bitrate'
    elif codec in {'flac', 'wave', 'aiff', 'monkeysaudio', 'wavpack'}:
        fallback = 'lossless_source'
    else:
        fallback = 'other_format'
    return dict(source_format=codec, source_bitrate=bitrate,
                source_sample_rate=int(getattr(audio.info, 'sample_rate', 0) or 0),
                source_channels=int(getattr(audio.info, 'channels', 0) or 0),
                source_bits_per_sample=int(getattr(audio.info, 'bits_per_sample', 0) or 0),
                source_duration=round(audio.info.length, 6),
                source_is_mp3_320=is_mp3_320, quality_fallback=fallback)


def safe_name(text):
    text = re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', str(text)).strip(' .')
    if text.upper().split('.')[0] in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        text = '_' + text
    return text or '未知歌曲'


def store_audio(source, out, stem):
    # Exclusive creation avoids accidental overwrite, even if a previous file has the same name.
    max_stem = min(130, max(20, 220 - len(str(out))))
    stem = safe_name(stem)[:max_stem].rstrip(' .')
    number = 1
    while True:
        suffix = '' if number == 1 else f' ({number})'
        destination = out / f'{stem}{suffix}{source.suffix.lower()}'
        try:
            with destination.open('xb') as dest, source.open('rb') as src:
                shutil.copyfileobj(src, dest)
            return destination
        except FileExistsError:
            number += 1
        except Exception:
            destination.unlink(missing_ok=True)
            raise


def download(args):
    session = read_json(args.session)
    if session.get('schema') != 1:
        raise ValueError('不支持的会话格式，请重新搜索')
    if time.time() - session['created'] > 86400:
        raise ValueError('搜索结果超过 24 小时，链接可能过期；请重新搜索并选歌')
    choices = {str(c['id']): c for c in session['candidates']}
    ids = list(dict.fromkeys(i.strip() for i in args.ids.split(',') if i.strip()))
    if not ids or any(i not in choices for i in ids):
        raise ValueError('编号不存在；请使用当前会话中展示的歌曲编号')
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    history_path = out / '.music-download-history.json'
    history = read_json(history_path) if history_path.exists() else {}
    report = []
    deps = dependencies(args.deps)
    if deps and deps not in sys.path:
        sys.path.insert(0,deps)
    with tempfile.TemporaryDirectory(prefix='music-download-', dir=out) as temp:
        for number in ids:
            item = choices[number]
            record = item['record']
            key = record_key(record)
            old = history.get(key, {})
            old_path = Path(old.get('path', ''))
            if (old.get('acquisition_policy') == 'mp3-first-v3'
                    and old_path.is_relative_to(out) and old_path.is_file() and old.get('sha256') == digest(old_path)):
                cached_info = None
                with contextlib.suppress(Exception):
                    cached_info = inspect_download(old_path,
                        record.get('duration_s') if record.get('metadata_only') else None)
                if cached_info and (cached_info['source_is_mp3_320'] or (
                        cached_info['source_format'] == 'flac' and 0 < cached_info['source_sample_rate'] <= 48000
                        and 0 < cached_info['source_bits_per_sample'] <= 16 and 0 < cached_info['source_channels'] <= 2)):
                    report.append({'id': int(number), 'song': record.get('song_name'), 'status': 'skipped', 'path': str(old_path)})
                    report[-1].update(cached_info)
                    continue
            result = run_job({'action': 'download', 'source': record['source'], 'record': record},
                             deps, args.timeout, Path(temp) / number)
            entry = {'id': int(number), 'song': record.get('song_name'), 'status': 'failed'}
            try:
                if not result['ok']:
                    for field in ('error_kind', 'available_bitrates', 'http_status', 'retry_after_seconds', 'resolution_attempts'):
                        if field in result:
                            entry[field] = result[field]
                    raise ValueError(result.get('error', '下载失败'))
                paths = [Path(p).resolve() for p in result.get('paths', [])]
                root = (Path(temp) / number / 'audio').resolve()
                if len(paths) != 1 or not paths[0].is_relative_to(root) or not audio_valid(paths[0]):
                    raise ValueError('文件未通过基本音频文件头检查，未计入成功')
                source_path = paths[0]
                if deps and deps not in sys.path:
                    sys.path.insert(0,deps)
                audio_info = inspect_download(source_path,
                    record.get('duration_s') if record.get('metadata_only') else None)
                if record.get('metadata_only') and result.get('resolution_phase') not in {'mp3_320', 'ordinary_flac'}:
                    raise ValueError('Selected metadata audio has no verified acquisition quality policy')
                if result.get('resolution_phase') == 'mp3_320' and not audio_info['source_is_mp3_320']:
                    raise ValueError('Source quality changed after resolution; expected real 320 kbps MP3')
                if result.get('resolution_phase') == 'ordinary_flac' and not (
                        audio_info['source_format'] == 'flac' and audio_info['source_sample_rate'] <= 48000
                        and audio_info['source_channels'] <= 2 and audio_info['source_bits_per_sample'] <= 16):
                    raise ValueError('Source format changed after resolution; expected ordinary FLAC')
                singers = record.get('singers') or '未知歌手'
                if isinstance(singers, list):
                    singers = '&'.join(str(s) for s in singers)
                stem = f"{int(number):02d}-{record.get('song_name') or '未知歌曲'}-{singers}-{LABELS.get(record['source'], record['source'])}"
                target = store_audio(source_path, out, stem)
                entry.update(status='downloaded', path=str(target), bytes=target.stat().st_size, validation='metadata-and-duration',
                             resolve_seconds=result.get('resolve_seconds',0), resolution_strategy=result.get('resolution_strategy'))
                entry.update(audio_info)
                for field in ('resolution_provider', 'requested_quality', 'resolution_phase', 'resolution_attempts'):
                    if field in result:
                        entry[field] = result[field]
                entry['download_seconds'] = result.get('download_seconds')
                history[key] = {'path': str(target), 'sha256': digest(target),
                                'acquisition_policy':('mp3-first-v3' if result.get('resolution_phase') in
                                    {'mp3_320', 'ordinary_flac'} else 'legacy-session'), 'audio_info':audio_info}
                write_json(history_path, history)
            except Exception as exc:
                entry['error'] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__ + '：本地保存失败'
            report.append(entry)
            write_json(out / 'download-report.json', {'out': str(out), 'results': report})
    counts = {status: sum(r['status'] == status for r in report) for status in ('downloaded', 'skipped', 'failed')}
    write_json(out / 'download-report.json', {'out':str(out),'results':report})
    emit({'out': str(out), 'counts': counts, 'results': report})
    return 2 if counts['failed'] else 0


def doctor(args):
    dep = dependencies(args.deps)
    env = dict(os.environ, PYTHONUTF8='1')
    if dep:
        env['PYTHONPATH'] = dep + os.pathsep + env.get('PYTHONPATH', '')
    code = 'import json; from musicdl import musicdl, __version__; from musicdl.modules import SongInfo; print(json.dumps({"version":__version__,"sources":list(musicdl.MusicClientBuilder.REGISTERED_MODULES)}))'
    try:
        process = subprocess.run([sys.executable, '-X', 'utf8', '-c', code], env=env, capture_output=True, text=True, encoding='utf-8', timeout=40)
        info = json.loads(process.stdout.strip().splitlines()[-1]) if process.returncode == 0 else None
    except (subprocess.TimeoutExpired, ValueError, IndexError):
        info = None
    emit({'ready': bool(info), 'python': sys.executable, 'deps': dep, 'musicdl': info,
          'install_if_missing': 'Run <skill-folder>/scripts/bootstrap.py, then scripts/runtime.py --doctor' if not info else None})
    return 0 if info else 2


def main():
    parser = argparse.ArgumentParser(description='先展示候选歌曲，再按编号下载；不剪辑、不覆盖已有文件')
    sub = parser.add_subparsers(dest='command', required=True)
    diag = sub.add_parser('doctor')
    diag.add_argument('--deps')
    sub.add_parser('sources')
    for action in ('search', 'playlist'):
        p = sub.add_parser(action)
        p.add_argument('--query', action='append', required=True) if action == 'search' else p.add_argument('--url', required=True)
        p.add_argument('--sources', default='QQ')
        p.add_argument('--workers', type=int, default=1, choices=range(1, 5))
        p.add_argument('--metadata-only', action='store_true')
        p.add_argument('--limit', type=int, default=5, choices=range(1, 101))
        p.add_argument('--session', type=Path, required=True)
        p.add_argument('--timeout', type=int, default=60, choices=range(1, 601))
        p.add_argument('--deps')
    p = sub.add_parser('download')
    p.add_argument('--session', type=Path, required=True)
    p.add_argument('--ids', required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--timeout', type=int, default=120, choices=range(1, 601))
    p.add_argument('--deps')
    p = sub.add_parser('_worker', help=argparse.SUPPRESS)
    p.add_argument('job')
    args = parser.parse_args()
    if args.command == '_worker':
        worker(args.job)
        return 0
    try:
        if args.command == 'sources':
            emit({'configured_sources': SOURCES, 'note': '可选适配器列表，不代表各平台当前可用；以搜索实测为准'})
            return 0
        if args.command == 'doctor':
            return doctor(args)
        if args.command in {'search', 'playlist'}:
            return search(args)
        return download(args)
    except (OSError, ValueError, KeyError) as exc:
        emit({'error': str(exc), 'type': type(exc).__name__})
        return 2


if __name__ == '__main__':
    sys.exit(main())
