#!/usr/bin/env python3
"""Offline audit reproductions for c76cf1f, NOT assertions of correct behaviour.

These tests PASS when the named defect is reproduced. Use synthetic files and
mock all network/cloud actions. Do not put this suite in the normal test gate.
Run: python3 review/reproduce_20261027.py
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, REPO / 'bin' / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


backup = module('review_backup', 'bonsai-backup.py')
watchdog = module('review_watchdog', 'bonsai-watchdog.py')
MODEL = backup.FILES[0]


def script(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/usr/bin/env bash\n' + text)
    path.chmod(0o700)
    return path


def clean_env(base, **extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('RUNPOD_', 'BONSAI_'))}
    env.update(HOME=str(base), PATH=str(base / 'mock') + ':' + os.environ['PATH'])
    env.update({k: str(v) for k, v in extra.items()})
    return env


def source_tree(base):
    root = base / 'source'
    main = root / MODEL
    main.parent.mkdir(parents=True)
    main.write_bytes(b'SYNTHETIC MODEL')
    (root / 'bin/cuda').mkdir(parents=True)
    return root


def make_archive(base, entries):
    archive, manifest = base / 'input.tar.gz', base / 'input.json'
    records = []
    with tarfile.open(archive, 'w:gz') as tar:
        for name, content in entries:
            item = tarfile.TarInfo(name)
            item.size = len(content)
            tar.addfile(item, io.BytesIO(content))
            records.append({'path': name, 'size': len(content),
                            'sha256': hashlib.sha256(content).hexdigest()})
    manifest.write_text(json.dumps({'format': 1, 'files': records}))
    return archive, manifest


def restore(archive, manifest, dest):
    return subprocess.run([sys.executable, str(REPO / 'bin/bonsai-restore.py'),
                           '--archive', str(archive), '--manifest', str(manifest),
                           '--destination', str(dest)], capture_output=True,
                          text=True, timeout=5)


class WatchdogReproductions(unittest.TestCase):
    def test_stop_failure_escapes_and_no_retry_occurs(self):
        argv = ['watchdog', '--execute-stop', '--pod-id', 'offline-test',
                '--idle-minutes', '0.001']
        failure = subprocess.CalledProcessError(1, ['fake-stop'])
        with patch.object(sys, 'argv', argv), \
             patch.object(watchdog.time, 'monotonic', side_effect=[0, 1]), \
             patch.object(watchdog, 'get_slot_state', return_value=False), \
             patch.object(watchdog.subprocess, 'run', side_effect=failure) as stop, \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(subprocess.CalledProcessError):
                watchdog.main()
        self.assertEqual(stop.call_count, 1)

    def test_restart_resets_hard_session_cap(self):
        original = watchdog.Watchdog(60, 120, 3600, 0)
        restarted = watchdog.Watchdog(60, 120, 3600, 3590)
        self.assertEqual(original.observe(3600, processing=True)[1],
                         'maximum-session-duration')
        self.assertIsNone(restarted.observe(3600, processing=True)[1])

    def test_idle_timer_uses_last_busy_poll_not_first_idle_poll(self):
        w = watchdog.Watchdog(60, 120, 3600, 0)
        w.observe(59, processing=True)
        w.observe(89, processing=False)
        # Only 30 seconds of observed inactivity, but configured timeout is 60.
        self.assertEqual(w.observe(119, processing=False)[1], 'idle-timeout')


class BackupRestoreReproductions(unittest.TestCase):
    def test_actual_projector_bias_and_library_links_omitted(self):
        with tempfile.TemporaryDirectory() as td:
            root = source_tree(Path(td))
            modeldir = root / 'models/bonsai2-gguf/27B'
            projector = modeldir / 'Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf'
            bias = modeldir / 'Bonsai-2-27B-kv-bias.gguf'
            projector.write_bytes(b'FAKE PROJECTOR')
            bias.write_bytes(b'FAKE BIAS')
            lib = root / 'bin/cuda/libggml.so.1'
            lib.write_bytes(b'FAKE LIBRARY')
            link = lib.with_name('libggml.so')
            link.symlink_to(lib.name)
            selected = set(backup.selected(root))
            self.assertIn(lib, selected)
            self.assertNotIn(projector, selected)
            self.assertNotIn(bias, selected)
            self.assertNotIn(link, selected)

    def test_secret_like_files_in_cuda_directory_are_included(self):
        with tempfile.TemporaryDirectory() as td:
            root = source_tree(Path(td))
            included = root / 'bin/cuda/server.env'
            excluded = root / 'bin/cuda/.env'
            included.write_text('FAKE_TOKEN=not-a-secret\n')
            excluded.write_text('FAKE_TOKEN=not-a-secret\n')
            self.assertIn(included, backup.selected(root))
            self.assertNotIn(excluded, backup.selected(root))

    def test_model_parent_symlink_can_escape_source_root(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / 'root'
            root.mkdir()
            external = base / 'outside'
            main = external / 'bonsai2-gguf/27B' / Path(MODEL).name
            main.parent.mkdir(parents=True)
            main.write_bytes(b'FAKE EXTERNAL FILE')
            (root / 'models').symlink_to(external, target_is_directory=True)
            chosen = backup.selected(root)
            self.assertEqual(len(chosen), 1)
            self.assertFalse(chosen[0].resolve().is_relative_to(root.resolve()))

    def test_cleanup_deletes_archive_created_by_concurrent_writer(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = source_tree(base)
            target = base / 'backup.tar.gz'
            def concurrent_writer(*args, **kwargs):
                target.write_bytes(b'OTHER WRITER SUCCESSFUL BACKUP')
                raise FileExistsError('Created after exists check, before x:gz open')
            argv = ['backup', '--root', str(root), '--output', str(target)]
            with patch.object(sys, 'argv', argv), \
                 patch.object(backup.tarfile, 'open', side_effect=concurrent_writer):
                with self.assertRaises(FileExistsError):
                    backup.main()
            self.assertFalse(target.exists())

    def test_source_change_between_hash_and_copy_invalidates_backup(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = source_tree(base)
            target = base / 'backup.tar.gz'
            main = root / MODEL
            original_digest = backup.digest
            def mutate_after_hash(path):
                result = original_digest(path)
                if path == main:
                    main.write_bytes(b'X' * main.stat().st_size)
                return result
            argv = ['backup', '--root', str(root), '--output', str(target)]
            with patch.object(sys, 'argv', argv), \
                 patch.object(backup, 'digest', side_effect=mutate_after_hash), \
                 contextlib.redirect_stdout(io.StringIO()):
                backup.main()
            result = restore(target, Path(str(target) + '.manifest.json'), base / 'restored')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Checksum mismatch', result.stderr)

    def test_restore_follows_symlink_destination(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            archive, manifest = make_archive(base, [('data.txt', b'SAFE DUMMY')])
            outside = base / 'outside'
            outside.mkdir()
            dest = base / 'restore'
            dest.symlink_to(outside, target_is_directory=True)
            result = restore(archive, manifest, dest)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((outside / 'data.txt').exists())

    def test_restore_file_directory_collision_leaves_partial_tree(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            archive, manifest = make_archive(base, [('a', b'ONE'), ('a/b', b'TWO')])
            dest = base / 'restore'
            result = restore(archive, manifest, dest)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((dest / 'a').read_bytes(), b'ONE')
            retry = restore(archive, manifest, dest)
            self.assertIn('Destination must be absent or empty', retry.stderr)


class LauncherReproductions(unittest.TestCase):
    def test_stale_pid_for_unrelated_process_blocks_server_start(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            state = base / 'run'
            state.mkdir()
            (state / 'server.pid').write_text(str(os.getpid()))
            script(base / 'mock/curl', 'exit 22\n')
            starter = script(base / 'starter', 'echo started > "$MARKER"\n')
            marker = base / 'marker'
            env = clean_env(base, BONSAI_ROOT=base, BONSAI_STATE_DIR=state,
                            BONSAI_STARTER=starter, MARKER=marker)
            result = subprocess.run(['bash', str(REPO / 'bin/bonsai-server-start')],
                                    env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('already running', result.stdout)
            self.assertFalse(marker.exists())

    def test_failed_background_server_is_reported_started(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            script(base / 'mock/curl', 'exit 22\n')
            starter = script(base / 'starter', 'echo START_FAILED >&2\nexit 7\n')
            result = subprocess.run(['bash', str(REPO / 'bin/bonsai-server-start')],
                                    env=clean_env(base, BONSAI_ROOT=base, BONSAI_STARTER=starter),
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Started Bonsai PID', result.stdout)
            for _ in range(50):
                log = base / 'run/server.log'
                if log.exists() and 'START_FAILED' in log.read_text():
                    break
                time.sleep(0.01)
            self.assertIn('START_FAILED', log.read_text())

    def test_cold_start_can_leave_server_without_watchdog(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            script(base / 'bin/bonsai-server-start', 'touch "$MARKER"\n')
            (base / 'bin/bonsai-watchdog.py').write_text('# never launched\n')
            script(base / 'mock/python3', 'echo "simulated slots HTTP 503" >&2\nexit 1\n')
            marker = base / 'server-started'
            result = subprocess.run(['bash', str(REPO / 'bin/bonsai-pod-services-start')],
                                    env=clean_env(base, BONSAI_ROOT=base, MARKER=marker),
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertTrue(marker.exists())
            self.assertFalse((base / 'run/watchdog.pid').exists())

    def test_custom_port_not_forwarded_to_watchdog(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            script(base / 'bin/bonsai-server-start', 'exit 0\n')
            (base / 'bin/bonsai-watchdog.py').write_text('# mock\n')
            script(base / 'mock/python3',
                   'if [[ "$1" == "-" ]]; then exit 0; fi\nprintf "%s\\n" "$@" > "$ARGS_FILE"\n')
            argsfile = base / 'args'
            result = subprocess.run(['bash', str(REPO / 'bin/bonsai-pod-services-start')],
                                    env=clean_env(base, BONSAI_ROOT=base, BONSAI_PORT='19080',
                                                  ARGS_FILE=argsfile),
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            for _ in range(50):
                if argsfile.exists(): break
                time.sleep(0.01)
            self.assertNotIn('--url', argsfile.read_text())
            self.assertNotIn('19080', argsfile.read_text())


class ControllerReproductions(unittest.TestCase):
    def test_status_checks_default_instead_of_configured_port(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = base / 'runpod.env'
            cfg.write_text('RUNPOD_POD_ID=offline-test\nRUNPOD_API_KEY=fake-only\nRUNPOD_LOCAL_PORT=19082\n')
            script(base / 'mock/curl',
                   'printf "%s\\n" "$@" >> "$CAPTURE"\n'
                   'if [[ "${!#}" == */health ]]; then exit 22; fi\n'
                   'printf \'%s\\n\' \'{"id":"offline-test","name":"test","status":"EXITED"}\'\n')
            capture = base / 'capture'
            result = subprocess.run(['bash', str(REPO / 'bin/runpod-bonsai'), 'status'],
                                    env=clean_env(base, RUNPOD_BONSAI_CONFIG=cfg, CAPTURE=capture),
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('127.0.0.1:18082/health', capture.read_text())
            self.assertNotIn('127.0.0.1:19082/health', capture.read_text())



class GitIgnoreReproduction(unittest.TestCase):
    def test_actual_key_name_and_backup_artifacts_not_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            subprocess.run(['git', 'init', '-q', str(base)], check=True,
                           capture_output=True, timeout=5)
            (base / '.gitignore').write_bytes((REPO / '.gitignore').read_bytes())
            for name in ('runpod_ed25519', 'bonsai-migration.tar.gz',
                         'bonsai-migration.tar.gz.manifest.json'):
                result = subprocess.run(['git', 'check-ignore', '--no-index', name],
                                        cwd=base, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 1, name)


if __name__ == '__main__':
    unittest.main(verbosity=2)
