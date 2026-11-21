"""Run a packaged tool in its isolated installed runtime without activation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

import bootstrap

HERE = Path(__file__).resolve().parent
TOOLS = ('music', 'preflight', 'pipeline', 'selftest', 'recover')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, default=HERE.parent / 'runtime.json')
    p.add_argument('--doctor', action='store_true')
    p.add_argument('--separator', action='store_true', help='With --doctor, also check AI dependencies')
    p.add_argument('--tool', choices=TOOLS)
    p.add_argument('arguments', nargs=argparse.REMAINDER)
    a = p.parse_args()
    if not a.config.is_file():
        p.error('Runtime config is missing. Run install.py or scripts/bootstrap.py first.')
    config = json.loads(a.config.read_text(encoding='utf-8-sig'))
    if config.get('schema_version') != 1:
        p.error('Unsupported runtime configuration; reinstall with this package.')
    if a.doctor:
        report = bootstrap.doctor(config['runtime_dir'], a.separator)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report['ok'] else 2
    if not a.tool:
        p.error('Use --doctor or --tool TOOL -- ARGUMENTS')
    python = Path(config['music_python'])
    if not python.is_file():
        p.error('Music runtime is not installed. Run scripts/bootstrap.py with the configured --runtime-dir.')
    arguments = a.arguments[1:] if a.arguments[:1] == ['--'] else a.arguments
    if a.tool == 'pipeline' and arguments[:1] == ['run'] and '--separator-python' not in arguments:
        separator = Path(config['separator_python'])
        if separator.is_file():
            arguments += ['--separator-python', str(separator)]
    return subprocess.run([str(python), '-s', '-X', 'utf8', str(HERE / (a.tool + '.py')), *arguments],
                          env=bootstrap.clean_environment()).returncode


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
