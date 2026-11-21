"""Offline portability and safety tests; uses only generated temporary folders."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[2]
SCRIPTS = PACKAGE / 'wedding-music-workflow' / 'scripts'
if not (PACKAGE / 'install.py').is_file():
    raise unittest.SkipTest('Installer tests run from the extracted release package; installed skills use scripts/selftest.py')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load('tested_installer', PACKAGE / 'install.py')
bootstrap = load('tested_bootstrap', SCRIPTS / 'bootstrap.py')


class PortableInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='portable 安装 ') 
        self.root = Path(self.temp.name)
        self.package = self.root / '解压包'
        self.source = self.package / installer.NAME
        (self.source / 'scripts').mkdir(parents=True)
        (self.source / 'SKILL.md').write_text('---\nname: wedding-music-workflow\ndescription: Test\n---\n', encoding='utf-8')
        shutil.copy2(SCRIPTS / 'bootstrap.py', self.source / 'scripts' / 'bootstrap.py')
        (self.source / 'runtime.json').write_text('{"secret": "DO NOT COPY"}', encoding='utf-8')
        (self.source / 'scripts' / '__pycache__').mkdir()
        (self.source / 'scripts' / '__pycache__' / 'old.pyc').write_bytes(b'ignore')
        self.args = argparse.Namespace(skills_dir=self.root / '新用户' / '.agents' / 'skills',
            runtime_dir=self.root / '新用户' / '运行 环境', skip_deps=True, separator=False,
            upgrade=False, dry_run=False)
        self.target = self.args.skills_dir / installer.NAME
        self.patched = patch.object(installer, 'HERE', self.package)
        self.patched.start()

    def tearDown(self):
        self.patched.stop()
        self.temp.cleanup()

    def test_dry_run_does_not_write(self):
        self.args.dry_run = True
        result = installer.install(self.args)
        self.assertTrue(result['dry_run'])
        self.assertFalse(self.args.skills_dir.exists())
        self.assertFalse(self.args.runtime_dir.exists())

    def test_skip_dependencies_rewrites_local_configuration(self):
        result = installer.install(self.args)
        self.assertTrue(result['ok'])
        config = json.loads((self.target / 'runtime.json').read_text(encoding='utf-8'))
        self.assertNotIn('secret', config)
        self.assertEqual(Path(config['runtime_dir']), self.args.runtime_dir.resolve())
        self.assertFalse((self.target / 'scripts' / '__pycache__').exists())
        self.assertFalse(self.args.runtime_dir.exists())

    def test_existing_install_requires_explicit_upgrade(self):
        installer.install(self.args)
        (self.target / 'keep.txt').write_text('old state', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            installer.install(self.args)
        self.assertEqual((self.target / 'keep.txt').read_text(), 'old state')

    def test_upgrade_keeps_external_backup(self):
        installer.install(self.args)
        (self.target / 'keep.txt').write_text('old state', encoding='utf-8')
        self.args.upgrade = True
        result = installer.install(self.args)
        backup = Path(result['backup'])
        self.assertFalse(backup.is_relative_to(self.args.skills_dir.resolve()))
        self.assertEqual((backup / 'keep.txt').read_text(), 'old state')
        self.assertFalse((self.target / 'keep.txt').exists())

    def test_upgrade_does_not_touch_unrelated_folder(self):
        self.target.mkdir(parents=True)
        (self.target / 'important.txt').write_text('keep', encoding='utf-8')
        self.args.upgrade = True
        with self.assertRaisesRegex(ValueError, 'not a recognized'):
            installer.install(self.args)
        self.assertEqual((self.target / 'important.txt').read_text(), 'keep')

    def test_cannot_install_on_source(self):
        self.args.skills_dir = self.package
        with self.assertRaisesRegex(ValueError, 'separate from'):
            installer.install(self.args)

    def test_runtime_must_stay_outside_skill(self):
        self.args.runtime_dir = self.target / '.runtime'
        with self.assertRaisesRegex(ValueError, 'must be separate'):
            installer.install(self.args)

    def test_failed_dependency_setup_preserves_existing_install(self):
        installer.install(self.args)
        (self.target / 'keep.txt').write_text('unchanged', encoding='utf-8')
        self.args.upgrade = True
        self.args.skip_deps = False
        with patch.object(installer.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['pip'])):
            with self.assertRaises(subprocess.CalledProcessError):
                installer.install(self.args)
        self.assertEqual((self.target / 'keep.txt').read_text(), 'unchanged')
        self.assertFalse(any(self.args.skills_dir.glob('.wedding-music-workflow-install-*')))

    def test_symbolic_link_target_is_rejected(self):
        self.args.skills_dir.mkdir(parents=True)
        other = self.root / 'unrelated'
        other.mkdir()
        try:
            self.target.symlink_to(other, target_is_directory=True)
        except OSError:
            self.skipTest('OS does not allow this user to create symlinks')
        with self.assertRaisesRegex(ValueError, 'link'):
            installer.install(self.args)


class RuntimeTests(unittest.TestCase):
    def test_actual_short_mp3_probe_accepts_header_rounding(self):
        for package in ('imageio_ffmpeg', 'mutagen', 'musicdl', 'numpy', 'soundfile'):
            if importlib.util.find_spec(package) is None:
                self.skipTest('Run in the installed music runtime to exercise the real MP3 probe')
        result = subprocess.run([sys.executable, '-s', '-X', 'utf8', '-c', bootstrap.MUSIC_DOCTOR],
            env=bootstrap.clean_environment(), capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(report['ok'])
        self.assertEqual(report['mp3_encoding_check']['bitrate_mode'], 'CBR')
        self.assertLessEqual(abs(report['mp3_encoding_check']['reported_bitrate'] - 320000), 100)

    def test_adding_ai_preserves_custom_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / 'runtime.json'
            custom = root / '自定义 运行环境'
            bootstrap.write_config(config, custom)
            self.assertEqual(bootstrap.configured_runtime(None, config), custom.resolve())
            override = root / 'override'
            self.assertEqual(bootstrap.configured_runtime(override, config), override)

    def test_environment_does_not_inherit_dependency_overrides(self):
        names = ('PYTHONPATH', 'PYTHONHOME', 'MUSIC_PICK_DEPS', 'PIP_TARGET', 'PIP_PREFIX', 'PIP_USER')
        with patch.dict(os.environ, {name: 'old-machine' for name in names}):
            env = bootstrap.clean_environment()
            self.assertTrue(all(name not in env for name in names))
            self.assertEqual(env['PYTHONNOUSERSITE'], '1')

    def test_missing_runtime_is_reported_without_installing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'missing'
            result = bootstrap.doctor(root, separator=True)
            self.assertFalse(result['ok'])
            self.assertEqual(set(result['checks']), {'music', 'separator'})
            self.assertFalse(root.exists())

    def test_setup_temp_applies_before_ensurepip(self):
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / 'runtime' / 'music'
            with patch.object(bootstrap.subprocess, 'run') as execute:
                bootstrap.build_environment(dest, ['fake-package==1'])
            self.assertEqual(execute.call_count, 2)
            first_env = execute.call_args_list[0].kwargs['env']
            self.assertEqual(Path(first_env['TEMP']), dest.parent / '.setup-temp')
            self.assertTrue(Path(first_env['TEMP']).is_dir())

    def test_wrapper_passes_arguments_and_cleans_environment(self):
        with tempfile.TemporaryDirectory(prefix='wrapper 中文 ') as temp:
            root = Path(temp)
            scripts = root / 'scripts'
            scripts.mkdir()
            for name in ('bootstrap.py', 'runtime.py'):
                shutil.copy2(SCRIPTS / name, scripts / name)
            (scripts / 'music.py').write_text(
                'import os,sys,json\nprint(json.dumps({"argv":sys.argv[1:],"leak":os.environ.get("PYTHONPATH")}))', encoding='utf-8')
            config = bootstrap.config_for(root / 'runtime')
            config['music_python'] = sys.executable
            (root / 'runtime.json').write_text(json.dumps(config), encoding='utf-8')
            env = dict(os.environ, PYTHONPATH='old-machine-path')
            result = subprocess.run([sys.executable, '-X', 'utf8', str(scripts / 'runtime.py'),
                '--tool', 'music', '--', '--example', '中文 with spaces'], capture_output=True,
                text=True, encoding='utf-8', env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data['argv'], ['--example', '中文 with spaces'])
            self.assertIsNone(data['leak'])


if __name__ == '__main__':
    unittest.main()
