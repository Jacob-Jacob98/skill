"""Create isolated, reproducible runtimes; never install into global Python."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

MUSIC_PACKAGES = [
    'musicdl==2.14.2', 'imageio-ffmpeg==0.6.0', 'mutagen==1.48.1',
    'numpy==2.5.4', 'soundfile==0.14.0',
]
SEPARATOR_PACKAGES = ['audio-separator[cpu]==0.47.0', 'audioread==3.1.0']
DEFAULT_RUNTIME = Path.home() / '.cache' / 'wedding-music-workflow' / 'v3'


def clean_environment():
    env = dict(os.environ)
    for key in ('PYTHONPATH', 'PYTHONHOME', 'MUSIC_PICK_DEPS', 'PIP_TARGET', 'PIP_PREFIX', 'PIP_USER'):
        env.pop(key, None)
    env['PYTHONNOUSERSITE'] = '1'
    env['PYTHONUTF8'] = '1'
    return env


def interpreter(directory):
    return Path(directory) / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def config_for(runtime_dir):
    root = Path(runtime_dir).expanduser().resolve()
    return {
        'schema_version': 1, 'package_version': '3.1.0',
        'runtime_dir': str(root), 'music_python': str(interpreter(root / 'music')),
        'separator_python': str(interpreter(root / 'separator')),
    }


def configured_runtime(explicit, config_path):
    if explicit is not None:
        return Path(explicit).expanduser()
    config_path = Path(config_path)
    if config_path.is_file():
        data = json.loads(config_path.read_text(encoding='utf-8-sig'))
        if data.get('schema_version') != 1 or not data.get('runtime_dir'):
            raise ValueError('Existing runtime configuration is invalid; specify --runtime-dir explicitly')
        return Path(data['runtime_dir']).expanduser()
    return DEFAULT_RUNTIME


def reject_link(path):
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        raise ValueError(f'Links or junctions are not accepted here: {path}')


def build_environment(directory, packages):
    raw = Path(directory).expanduser()
    reject_link(raw)
    dest = raw.resolve()
    if dest in (Path(dest.anchor), Path.home().resolve()):
        raise ValueError('Runtime cannot be a drive root or the home directory')
    if dest.exists() and (not dest.is_dir() or (any(dest.iterdir()) and not (dest / 'pyvenv.cfg').is_file())):
        raise ValueError(f'Runtime must be empty or an existing virtual environment: {dest}')
    env = clean_environment()
    temp = dest.parent / '.setup-temp'
    temp.mkdir(parents=True, exist_ok=True)
    env.update(TMP=str(temp), TEMP=str(temp), TMPDIR=str(temp))
    # Creation runs in a clean child so the caller cannot leak a PYTHONPATH into ensurepip.
    subprocess.run([sys.executable, '-I', '-m', 'venv', str(dest)], env=env, check=True)
    python = interpreter(dest)
    subprocess.run([
        str(python), '-I', '-m', 'pip', 'install', '--disable-pip-version-check',
        *packages,
    ], env=env, check=True)
    return python


MUSIC_DOCTOR = r'''
import importlib.metadata as md, json, pathlib, subprocess, tempfile
import imageio_ffmpeg, mutagen, musicdl, numpy, soundfile
from mutagen.mp3 import MP3, BitrateMode
versions = {p: md.version(p) for p in ('musicdl', 'imageio-ffmpeg', 'mutagen', 'numpy', 'soundfile')}
ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
with tempfile.TemporaryDirectory(prefix='music-runtime-check-') as d:
    output = pathlib.Path(d) / 'test.mp3'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
      'anullsrc=r=44100:cl=stereo', '-t', '0.2', '-c:a', 'libmp3lame',
      '-b:a', '320k', str(output)], check=True, capture_output=True)
    info = MP3(output).info
    # Tiny fixtures can report 319997 from container rounding despite 320k CBR frames.
    if (abs(info.bitrate - 320000) > 100 or info.bitrate_mode != BitrateMode.CBR
        or info.sample_rate != 44100 or info.channels != 2):
        raise RuntimeError('MP3 encoding check failed')
print(json.dumps({'ok': True, 'packages': versions, 'ffmpeg': ffmpeg,
  'mp3_encoding_check': {'target_bitrate': 320000, 'reported_bitrate': info.bitrate,
    'bitrate_mode': 'CBR', 'sample_rate': info.sample_rate, 'channels': info.channels}}, ensure_ascii=False))
'''

SEPARATOR_DOCTOR = r'''
import importlib.metadata as md, json
import audioread
from audio_separator.separator import Separator
import onnxruntime
print(json.dumps({'ok': True, 'packages': {p: md.version(p) for p in
  ('audio-separator', 'audioread', 'onnxruntime')},
  'providers': onnxruntime.get_available_providers(),
  'model_test': 'Not run; model is downloaded on first separation'}, ensure_ascii=False))
'''


def doctor(runtime_dir, separator=False):
    root = Path(runtime_dir).expanduser().resolve()
    checks = [('music', MUSIC_DOCTOR)]
    if separator:
        checks.append(('separator', SEPARATOR_DOCTOR))
    report = {'ok': True, 'checks': {}}
    for name, code in checks:
        python = interpreter(root / name)
        if not python.is_file():
            report['checks'][name] = {'ok': False, 'error': f'Runtime is not installed: {python}'}
            report['ok'] = False
            continue
        env = clean_environment()
        temp = root / '.setup-temp'
        temp.mkdir(parents=True, exist_ok=True)
        env.update(TMP=str(temp), TEMP=str(temp), TMPDIR=str(temp))
        result = subprocess.run([str(python), '-I', '-X', 'utf8', '-c', code],
                                env=env, capture_output=True, text=True, encoding='utf-8')
        try:
            value = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            value = {'ok': False, 'error': (result.stderr or result.stdout).strip()[-4000:]}
        value['ok'] = bool(value.get('ok')) and result.returncode == 0
        report['checks'][name] = value
        report['ok'] = report['ok'] and value['ok']
    return report


def write_config(path, runtime_dir):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config_for(runtime_dir), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--runtime-dir', type=Path, help='Default: existing runtime.json, then the user cache directory')
    p.add_argument('--config', type=Path, default=Path(__file__).resolve().parents[1] / 'runtime.json')
    p.add_argument('--separator', action='store_true', help='Also install/check the isolated CPU accompaniment runtime')
    p.add_argument('--doctor', action='store_true', help='Check only; do not install or download')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    if sys.version_info < (3, 12):
        p.error('Python 3.12+ is required; Python 3.12 is recommended')
    root = configured_runtime(args.runtime_dir, args.config)
    reject_link(root)
    root = root.resolve()
    if args.doctor:
        result = doctor(root, args.separator)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['ok'] else 2
    if args.dry_run:
        print(json.dumps({'config': config_for(root), 'music_packages': MUSIC_PACKAGES,
                          'separator_packages': SEPARATOR_PACKAGES if args.separator else []}, ensure_ascii=False, indent=2))
        return 0
    build_environment(root / 'music', MUSIC_PACKAGES)
    if args.separator:
        build_environment(root / 'separator', SEPARATOR_PACKAGES)
    result = doctor(root, args.separator)
    if result['ok']:
        write_config(args.config, root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
