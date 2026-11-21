"""Confirmed-song workflow; portable paths, isolated jobs, verified MP3 publication."""
import argparse
import concurrent.futures
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import uuid
import time

HERE = Path(__file__).resolve().parent

def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()

def job_digest(job):
    return hashlib.sha256(json.dumps(job,sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest()

def seconds(value):
    if value in (None, '', '从头开始'):
        return 0.0
    parts = str(value).split(':')
    if len(parts) > 3:
        raise ValueError('Invalid time')
    numbers = [float(x) for x in parts]
    if any(not math.isfinite(x) or x < 0 for x in numbers):
        raise ValueError('Time must be finite and nonnegative')
    if len(numbers) > 1 and any(x >= 60 for x in numbers[1:]):
        raise ValueError('Minutes/seconds component must be less than 60')
    result = 0.0
    for n in numbers:
        result = result*60+n
    return result

def clean(value):
    return re.sub(r'\s+', ' ', re.sub(r'[<>:"/\\|?*\x00-\x1f]', ' ', str(value))).strip(' .')

def filename(job):
    authors = job.get('artists', [])
    if isinstance(authors, str):
        authors = [authors]
    title = job['title']
    version = job.get('version', '')
    if job.get('separate') and 'AI分离' not in version:
        version = (version + ' ' if version else '') + '伴奏（AI分离）'
    if version:
        title += '（' + version + '）'
    name = f"{job['index']}-{clean(job['stage'])}《{clean(title)}》{clean(' '.join(authors))}.mp3"
    if len(name) > 180:
        raise ValueError('Filename too long; ask user for a shorter name')
    return name

def manifest(path):
    result = load(path)
    if not isinstance(result, dict) or not isinstance(result.get('tracks'), list) or not result['tracks']:
        raise ValueError('Manifest must contain a nonempty tracks array')
    seen, names = set(), set()
    for j in result['tracks']:
        n = j.get('index')
        if isinstance(n, bool) or not isinstance(n, int) or n < 1 or n in seen:
            raise ValueError('Track indices must be unique positive integers')
        seen.add(n)
        if not j.get('title') or not isinstance(j.get('stage'), str):
            raise ValueError('Each track needs a title and a stage string (empty when omitted by the user)')
        start = seconds(j.get('start', 0))
        if j.get('end') is not None and seconds(j['end']) <= start:
            raise ValueError('End time must be later than start')
        name = filename(j)
        if name.casefold() in names:
            raise ValueError('Duplicate filenames')
        names.add(name.casefold())
    return result

def absolute(value, base):
    p = Path(value)
    return p.resolve() if p.is_absolute() else (base/p).resolve()

def ffmpeg_path(explicit=None):
    if explicit:
        return str(Path(explicit).resolve())
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()

def inspect_audio(path):
    from mutagen import File
    obj = File(path)
    if obj is None or not hasattr(obj.info, 'length'):
        raise ValueError('Audio metadata could not be read')
    return obj.info

def execute(cmd, timeout):
    subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)

def verified_flac_pcm(source):
    """Recover container-only FLAC errors only when every PCM sample matches STREAMINFO MD5."""
    import soundfile as sf
    from mutagen.flac import FLAC
    info = FLAC(source).info
    if info.bits_per_sample not in (16,24) or not info.md5_signature:
        raise ValueError('FLAC recovery requires a supported PCM depth and stored MD5')
    temp = source.with_name('verified-pcm-'+uuid.uuid4().hex+'.wav')
    checksum, count = hashlib.md5(), 0
    try:
        with sf.SoundFile(str(source)) as inp, sf.SoundFile(str(temp),'w',samplerate=info.sample_rate,
                channels=info.channels,subtype='PCM_16' if info.bits_per_sample==16 else 'PCM_24') as out:
            for block in inp.blocks(blocksize=65536,dtype='int32',always_2d=True):
                count += len(block)
                if info.bits_per_sample == 16:
                    packed = (block >> 16).astype('<i2').tobytes()
                else:
                    packed = (block >> 8).astype('<i4').view('uint8').reshape(-1,4)[:,:3].tobytes()
                checksum.update(packed)
                out.write(block)
        if count != info.total_samples or checksum.hexdigest()!=format(info.md5_signature,'032x'):
            raise ValueError('FLAC PCM checksum or sample count mismatch; do not repair')
        return temp
    except Exception:
        temp.unlink(missing_ok=True)
        raise

def encode(source, dest, job, ffmpeg, validation='strict'):
    from mutagen.mp3 import MP3, BitrateMode
    started = time.perf_counter()
    source_info = inspect_audio(source)
    original = source_info.length
    start = seconds(job.get('start', 0))
    end = seconds(job['end']) if job.get('end') is not None else original
    if start >= original or end > original+0.08 or end <= start:
        raise ValueError('Trim interval outside source duration')
    trimmed = start > 0 or job.get('end') is not None
    if trimmed and end-start < 0.3:
        raise ValueError('Trimmed audio is shorter than the required 0.3-second fade')
    from mutagen import File
    source_is_mp3 = isinstance(File(source), MP3)
    compliant = (source_is_mp3 and source_info.bitrate == 320000
                 and source_info.bitrate_mode == BitrateMode.CBR
                 and source_info.sample_rate == 44100 and source_info.channels == 2)
    mode = 'copy' if compliant and not trimmed else 'encode'
    restored_padding = 0
    if mode == 'copy':
        shutil.copy2(source, dest)
    else:
        cmd = [ffmpeg, '-v', 'error', '-xerror', '-y']
        if start:
            cmd += ['-ss', str(start)]
        cmd += ['-i', str(source), '-map', '0:a:0', '-vn', '-map_metadata', '-1']
        if trimmed:
            cmd += ['-af', f'atrim=duration={end-start},asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.3']
        cmd += ['-c:a','libmp3lame','-b:a','320k','-ar','44100','-ac','2',str(dest)]
        try:
            execute(cmd, 300)
        except subprocess.CalledProcessError:
            if source_is_mp3:
                from mp3_padding import recover_mp3_tail
                repaired = dest.with_name('verified-mp3-'+uuid.uuid4().hex+'.mp3')
                try:
                    evidence = recover_mp3_tail(source, repaired)
                    execute([ffmpeg,'-v','error','-xerror','-i',str(repaired),'-map','0:a:0','-f','null','-'],300)
                    cmd[cmd.index('-i')+1] = str(repaired)
                    execute(cmd, 300)
                    restored_padding = evidence['padding_added']
                    mode = 'encode-restored-mp3-padding'
                finally:
                    repaired.unlink(missing_ok=True)
            elif source.suffix.lower() != '.flac':
                raise
            else:
                repaired = verified_flac_pcm(source)
                try:
                    cmd[cmd.index('-i')+1] = str(repaired)
                    execute(cmd, 300)
                    mode = 'encode-verified-flac-pcm'
                finally:
                    repaired.unlink(missing_ok=True)
    encoded_at = time.perf_counter()
    full_decode = validation == 'strict' or mode == 'copy'
    if full_decode:
        try:
            execute([ffmpeg,'-v','error','-xerror','-i',str(dest),'-map','0:a:0','-f','null','-'],300)
        except subprocess.CalledProcessError:
            if mode != 'copy':
                raise
            from mp3_padding import recover_mp3_tail
            repaired = dest.with_name('verified-mp3-'+uuid.uuid4().hex+'.mp3')
            try:
                evidence = recover_mp3_tail(source, repaired)
                execute([ffmpeg,'-v','error','-xerror','-i',str(repaired),'-map','0:a:0','-f','null','-'],300)
                repaired.replace(dest)
                restored_padding = evidence['padding_added']
                mode = 'copy-restored-mp3-padding'
            finally:
                repaired.unlink(missing_ok=True)
    info = inspect_audio(dest)
    if (info.bitrate != 320000 or info.sample_rate != 44100 or info.channels != 2
            or info.bitrate_mode != BitrateMode.CBR or abs(info.length-(end-start)) > 0.2):
        raise ValueError('Output bitrate or duration validation failed')
    return dict(duration=info.length, original_duration=original, start=start, end=end,
                fade_in=0.3 if trimmed else 0, bitrate=info.bitrate, full_decode=full_decode,
                processing_mode=mode, validation=validation,
                restored_mp3_padding_bytes=restored_padding,
                processing_seconds=round(encoded_at-started,3),
                validation_seconds=round(time.perf_counter()-encoded_at,3))

def separate(source, jobdir, args, ffmpeg):
    if not args.separator_python:
        raise ValueError('AI separation requested; install separator environment and pass --separator-python')
    # Supply a conventional FFmpeg executable name without changing system PATH.
    binary = args.work/'ffmpeg-bin'
    binary.mkdir(exist_ok=True)
    exe = binary/('ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
    if not exe.exists():
        shutil.copy2(ffmpeg, exe)
    wav = jobdir/'separation-input.wav'
    execute([ffmpeg,'-v','error','-y','-i',str(source),'-map','0:a:0','-vn','-ar','44100','-ac','2','-c:a','pcm_s16le',str(wav)],300)
    env = dict(os.environ, PATH=str(binary.resolve())+os.pathsep+os.environ.get('PATH',''))
    # A separate Python environment must not inherit the music runtime's packages.
    env.pop('PYTHONPATH', None)
    cmd = [str(args.separator_python),'-X','utf8',str(HERE/'separate.py'),'--input',str(wav),'--out',str(jobdir/'separated'),
           '--models',str(args.work/'models'),'--model',args.model]
    with (jobdir/'separation.log').open('w',encoding='utf-8') as log:
        subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, check=True, timeout=args.separation_timeout)
    lines = (jobdir/'separation.log').read_text(encoding='utf-8',errors='replace').splitlines()
    data = next((json.loads(x) for x in reversed(lines) if x.startswith('{') and '"instrumental"' in x), None)
    if not data:
        raise ValueError('Separator did not confirm instrumental output')
    out = Path(data['instrumental']).resolve()
    if not out.is_relative_to((jobdir/'separated').resolve()) or not out.is_file():
        raise ValueError('Invalid separated file')
    if abs(inspect_audio(out).length-inspect_audio(source).length) > 0.2:
        raise ValueError('Separated audio duration differs from source')
    return out

@contextlib.contextmanager
def batch_lock(work):
    """One runner/publisher per batch; do not silently overwrite a live report."""
    work.mkdir(parents=True, exist_ok=True)
    lock = work/'.run.lock'
    try:
        stream = lock.open('x', encoding='utf-8')
    except FileExistsError:
        raise ValueError('Batch is already in use; choose another work folder or inspect its lock') from None
    try:
        with stream:
            json.dump({'pid':os.getpid(), 'started':time.time()}, stream)
        yield
    finally:
        lock.unlink(missing_ok=True)


def run(args):
    with batch_lock(args.work):
        return run_batch(args)


def run_batch(args):
    data = manifest(args.manifest)
    base = args.manifest.parent
    args.work.mkdir(parents=True,exist_ok=True)
    final = args.work/'final'
    final.mkdir(exist_ok=True)
    ffmpeg = ffmpeg_path(args.ffmpeg)
    separation_slot = threading.Semaphore(1)
    batch_started = time.perf_counter()
    process_workers = getattr(args, 'process_workers', 1)
    if not 1 <= args.download_workers <= 3 or not 1 <= process_workers <= 2:
        raise ValueError('Invalid download or processing worker limit')
    counters = {'active_downloads':0, 'active_processing':0,
                'peak_downloads':0, 'peak_processing':0}
    counter_lock = threading.Lock()

    @contextlib.contextmanager
    def counted(stage):
        with counter_lock:
            counters['active_'+stage] += 1
            counters['peak_'+stage] = max(counters['peak_'+stage], counters['active_'+stage])
        try:
            yield
        finally:
            with counter_lock:
                counters['active_'+stage] -= 1

    def acquire(job):
        started = time.perf_counter()
        n = job['index']
        result = dict(index=n, file=filename(job), status='pending', job_digest=job_digest(job),
                      workflow_policy='mp3-first-v3')
        if job.get('confirmed') is not True:
            result['reason'] = 'Version selection requires user confirmation'
            return None, result
        folder = args.work/'jobs'/str(n)
        folder.mkdir(parents=True,exist_ok=True)
        dest = final/result['file']
        try:
            previous_path = folder/'result.json'
            if not args.fresh and previous_path.exists() and dest.is_file():
                previous = load(previous_path)
                source_unchanged = (not job.get('source_file') or
                    previous.get('source_sha256') == digest(absolute(job['source_file'],base)))
                if (source_unchanged and previous.get('status') == 'verified'
                        and previous.get('workflow_policy') == result['workflow_policy']
                        and previous.get('job_digest') == result['job_digest']
                        and previous.get('sha256') == digest(dest)
                        and (args.validation != 'strict' or previous.get('full_decode'))):
                    previous.update(resumed=True, total_seconds=round(time.perf_counter()-started,3))
                    return None, previous
            if job.get('source_file'):
                source = absolute(job['source_file'],base)
            else:
                session = absolute(job['session'],base)
                cmd = [sys.executable,'-X','utf8',str(HERE/'music.py'),'download','--session',str(session),'--ids',str(job['candidate_id']),
                       '--out',str(folder/('download-'+uuid.uuid4().hex if args.fresh else 'download')),'--timeout',str(args.download_timeout)]
                download_folder = Path(cmd[cmd.index('--out')+1])
                if args.deps:
                    cmd += ['--deps',str(args.deps)]
                with counted('downloads'), (folder/'download.log').open('w',encoding='utf-8') as log:
                    proc = subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,timeout=args.download_timeout+60)
                if proc.returncode:
                    failure_report = download_folder/'download-report.json'
                    if failure_report.is_file():
                        failures = load(failure_report).get('results', [])
                        if len(failures) == 1:
                            for field in ('error_kind', 'available_bitrates', 'http_status', 'retry_after_seconds', 'resolution_attempts'):
                                if field in failures[0]:
                                    result[field] = failures[0][field]
                            raise ValueError(failures[0].get('error') or 'Download failed; inspect per-job report')
                    raise ValueError('Download failed; inspect sanitized per-job report')
                downloaded = load(download_folder/'download-report.json')['results']
                if len(downloaded) != 1 or downloaded[0]['status'] not in ('downloaded','skipped'):
                    raise ValueError('No successful audio download')
                source = Path(downloaded[0]['path']).resolve()
                result['resolve_seconds'] = downloaded[0].get('resolve_seconds',0)
                result['download_seconds'] = downloaded[0].get('download_seconds')
                result['resolution_strategy'] = downloaded[0].get('resolution_strategy')
                for field in ('source_format', 'source_bitrate', 'source_sample_rate', 'source_channels', 'source_bits_per_sample',
                              'source_duration', 'source_is_mp3_320', 'quality_fallback',
                              'resolution_provider', 'requested_quality', 'resolution_phase', 'resolution_attempts'):
                    if field in downloaded[0]:
                        result[field] = downloaded[0][field]
                if not source.is_relative_to(download_folder.resolve()):
                    raise ValueError('Downloaded file outside job directory')
            result['source_sha256'] = digest(source)
            result['acquire_seconds'] = round(time.perf_counter()-started,3)
            result['status'] = 'acquired'
            return (job, folder, dest, source, started, time.perf_counter()), result
        except Exception as exc:
            result.update(status='failed',reason=str(exc) if isinstance(exc,ValueError) else type(exc).__name__)
            result['total_seconds'] = round(time.perf_counter()-started,3)
            return None, result

    def process(prepared, result):
        job, folder, dest, source, started, ready_at = prepared
        result = dict(result)
        result['processing_queue_seconds'] = round(time.perf_counter()-ready_at,3)
        try:
            separation_started = time.perf_counter()
            if job.get('separate'):
                with separation_slot:
                    source = separate(source,folder,args,ffmpeg)
            result['separation_seconds'] = round(time.perf_counter()-separation_started,3)
            with counted('processing'):
                info = encode(source,dest,job,ffmpeg,args.validation)
            result.update(status='verified',sha256=digest(dest),**info)
        except Exception as exc:
            # Commands can contain private paths; retain detailed logs locally, not in user output.
            result.update(status='failed',reason=str(exc) if isinstance(exc,ValueError) else type(exc).__name__)
            if dest.exists():
                dest.unlink()
        result['total_seconds'] = round(time.perf_counter()-started,3)
        return result

    # One acquisition queue preserves serial downloads by default. Processing
    # uses a separate pool so song N can encode while song N+1 downloads.
    latest = {j['index']:dict(index=j['index'], file=filename(j), status='queued') for j in data['tracks']}
    def snapshot():
        save(args.work/'report.json', {
            'results':[latest[j['index']] for j in data['tracks']],
            'elapsed_seconds':round(time.perf_counter()-batch_started,3),
            'settings':{'download_workers':args.download_workers, 'process_workers':process_workers},
            'peak_downloads':counters['peak_downloads'], 'peak_processing':counters['peak_processing']})
    def completed(job, result):
        folder = args.work/'jobs'/str(job['index'])
        save(folder/'result.json',result)
        latest[job['index']] = result
        snapshot()
        print(json.dumps(result,ensure_ascii=False),flush=True)
    snapshot()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.download_workers) as downloads, \
            concurrent.futures.ThreadPoolExecutor(max_workers=process_workers) as processing:
        futures = {downloads.submit(acquire,j):('acquire',j) for j in data['tracks']}
        while futures:
            done, _ = concurrent.futures.wait(futures, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                stage, job = futures.pop(future)
                try:
                    value = future.result()
                except Exception as exc:
                    completed(job, dict(index=job['index'],file=filename(job),status='failed',
                        job_digest=job_digest(job),reason=type(exc).__name__))
                    continue
                if stage == 'acquire':
                    prepared, result = value
                    if prepared is not None:
                        latest[job['index']] = result
                        snapshot()
                        futures[processing.submit(process,prepared,result)] = ('process',job)
                        continue
                else:
                    result = value
                completed(job,result)
    results = [latest[j['index']] for j in data['tracks']]
    snapshot()
    return 0 if all(r['status']=='verified' for r in results) else 2

def publish(args):
    with batch_lock(args.work):
        return publish_batch(args)


def publish_batch(args):
    data = manifest(args.manifest)
    results = {r['index']:r for r in load(args.work/'report.json')['results']}
    wanted = []
    for j in data['tracks']:
        r = results.get(j['index'],{})
        src = args.work/'final'/filename(j)
        if j.get('confirmed') is not True or r.get('status')!='verified' or r.get('job_digest')!=job_digest(j) or r.get('file')!=src.name or not src.is_file() or digest(src)!=r.get('sha256'):
            raise ValueError('Not all manifest tracks have verified matching MP3 files')
        wanted.append(src)
    if args.out.is_symlink() or (hasattr(args.out,'is_junction') and args.out.is_junction()):
        raise ValueError('Final folder must not be a link or junction')
    dest = args.out.resolve()
    home = Path.home().resolve()
    if dest==Path(dest.anchor) or dest==home or args.work==dest or args.work.is_relative_to(dest) or dest.is_relative_to(args.work):
        raise ValueError('Unsafe final folder; it must be separate from work, home and drive roots')
    if dest.is_symlink():
        raise ValueError('Final folder must not be a symlink')
    dest.mkdir(parents=True,exist_ok=True)
    old = list(dest.iterdir())
    if old and not args.replace_confirmed:
        raise ValueError('Output folder is not empty; obtain authorization for external backup and replacement')
    backup = args.work/('previous-output-'+uuid.uuid4().hex)
    moved, copied = [], []
    try:
        if old:
            backup.mkdir()
        for p in old:
            if p.is_symlink() or not p.resolve().is_relative_to(dest):
                raise ValueError('Existing entry points outside destination; leave it unchanged')
        for p in old:
            shutil.move(str(p),str(backup/p.name))
            moved.append(p.name)
        for src in wanted:
            dst = dest/src.name
            if dst.exists():
                raise ValueError('Destination changed during publication')
            shutil.copy2(src,dst)
            copied.append(dst)
            if digest(dst)!=digest(src):
                raise ValueError('Copied file verification failed')
        if {p.name for p in dest.iterdir()}!={p.name for p in wanted}:
            raise ValueError('Unexpected files appeared in final folder')
    except Exception:
        for p in copied:
            if p.is_file():
                p.unlink()
        for name in moved:
            if not (dest/name).exists():
                shutil.move(str(backup/name),str(dest/name))
        raise
    print(json.dumps({'out':str(dest),'mp3_count':len(wanted),'backup':str(backup) if old else None},ensure_ascii=False))
    return 0

def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='command',required=True)
    for action in ('run','publish'):
        s = sub.add_parser(action)
        s.add_argument('--manifest',type=Path,required=True)
        s.add_argument('--work',type=Path,required=True)
        if action=='run':
            s.add_argument('--deps',type=Path)
            s.add_argument('--ffmpeg')
            s.add_argument('--download-workers',type=int,choices=range(1,4),default=1)
            s.add_argument('--process-workers',type=int,choices=range(1,3),default=1)
            s.add_argument('--fresh',action='store_true',help='Disable resume and download history reuse')
            s.add_argument('--validation',choices=('strict','balanced'),default='strict')
            s.add_argument('--download-timeout',type=int,default=300)
            s.add_argument('--separator-python',type=Path)
            s.add_argument('--model',default='UVR-MDX-NET-Inst_HQ_3.onnx')
            s.add_argument('--separation-timeout',type=int,default=3600)
        else:
            s.add_argument('--out',type=Path,required=True)
            s.add_argument('--replace-confirmed',action='store_true')
    a = p.parse_args()
    a.work, a.manifest = a.work.resolve(), a.manifest.resolve()
    if getattr(a,'deps',None):
        a.deps = a.deps.resolve()
        sys.path.insert(0,str(a.deps))
    if getattr(a,'separator_python',None):
        a.separator_python = a.separator_python.resolve()
    try:
        return run(a) if a.command=='run' else publish(a)
    except Exception as e:
        print(json.dumps({'error':str(e),'type':type(e).__name__},ensure_ascii=False))
        return 2

if __name__=='__main__':
    sys.exit(main())
