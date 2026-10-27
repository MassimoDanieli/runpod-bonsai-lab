#!/usr/bin/env python3
"""Offline Linux process tests: loopback fake server, real dry-run watchdog.

Never loads GGUF/CUDA, opens SSH, or supplies a RunPod credential. API calls are
not enabled. Every created process and file belongs to the synthetic fixture.
"""
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'bin/bonsai-pod.py'
spec = importlib.util.spec_from_file_location('bonsai_pod', SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

FAKE_SERVER = r'''import http.server,json,os,sys,time
from pathlib import Path
root=Path(__file__).parent
cfg=json.loads((root/'fake.json').read_text())
with (root/'events').open('a') as out:
    out.write('server-start\n')
(root/'server-env.json').write_text(json.dumps(dict(os.environ)))
if cfg.get('exit'):
    sys.exit(7)
if cfg.get('noise'):
    print('X'*cfg['noise'],flush=True)
port=int(sys.argv[sys.argv.index('--port')+1])
started=time.monotonic()
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if time.monotonic()-started < cfg.get('ready_after',0):
            self.send_response(503); self.end_headers(); return
        payload={'status':'ok'} if self.path=='/health' else [{'is_processing':False}]
        if self.path=='/slots' and cfg.get('bad_slots'):
            payload={'error':'disabled'}
        raw=json.dumps(payload).encode()
        self.send_response(200);self.end_headers();self.wfile.write(raw)
    def log_message(self,*_): pass
class Server(http.server.HTTPServer): allow_reuse_address=True
Server(('127.0.0.1',port),Handler).serve_forever()
'''


class UtilitiesTests(unittest.TestCase):
    def test_live_identity_contains_start_time(self):
        got = mod.identity(os.getpid())
        self.assertTrue(mod.matches(got))
        self.assertGreater(got['start_ticks'], 0)
        self.assertEqual(set(got), mod.IDENTITY_KEYS)

    def test_wrong_start_time_is_not_identity(self):
        got = mod.identity(os.getpid())
        got['start_ticks'] += 1
        self.assertFalse(mod.matches(got))
        with patch.object(mod.signal, 'pidfd_send_signal') as send:
            self.assertFalse(mod.signal_owned(got, signal.SIGTERM))
            send.assert_not_called()

    def test_identity_rechecked_after_pidfd_open(self):
        got = mod.identity(os.getpid())
        with patch.object(mod, 'matches', side_effect=[True, False]), patch.object(mod.signal, 'pidfd_send_signal') as send:
            self.assertFalse(mod.signal_owned(got, signal.SIGTERM))
            send.assert_not_called()

    def test_missing_and_invalid_pid(self):
        self.assertIsNone(mod.identity(0))
        self.assertIsNone(mod.identity(True))
        self.assertIsNone(mod.identity(99999999))
        self.assertFalse(mod.matches({'pid': os.getpid()}))

    def test_exclusive_lock_rejects_second_writer(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'lock'
            with mod.lock_file(path):
                with self.assertRaises(mod.SafetyError):
                    with mod.lock_file(path):
                        pass
            self.assertTrue(path.exists())
            with mod.lock_file(path):
                pass

    def test_log_rotation_is_bounded_for_live_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'model.log'
            log = mod.RotatingLog(path, 1024)
            log.write(b'x' * 9000)
            log.close()
            files = list(Path(temp).iterdir())
            self.assertEqual(len(files), 3)
            self.assertTrue(all(p.stat().st_size <= 1024 for p in files))
            self.assertTrue(all(p.stat().st_mode & 0o077 == 0 for p in files))

    def test_log_rotation_counts_previous_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'model.log'
            path.write_bytes(b'a' * 1024)
            path.chmod(0o600)
            log = mod.RotatingLog(path, 1024)
            log.write(b'b')
            log.close()
            self.assertEqual(path.read_bytes(), b'b')
            self.assertEqual(Path(str(path) + '.1').read_bytes(), b'a' * 1024)

    def test_runtime_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'real').mkdir()
            (root / 'link').symlink_to(root / 'real')
            with self.assertRaises(mod.SafetyError):
                mod.private_path(root / 'link/run', directory=True)

    def test_public_runtime_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'public'
            path.mkdir(mode=0o755)
            with self.assertRaises(mod.SafetyError):
                mod.private_path(path, directory=True)

    def test_private_json_rejects_corruption(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'state'
            path.write_text('bad json')
            path.chmod(0o600)
            with self.assertRaises(mod.SafetyError):
                mod.load_json(path)

    def test_nan_and_invalid_ports_rejected(self):
        for raw in ('nan', 'inf', '-1', '99999'):
            with patch.dict(os.environ, {'TEST_VALUE': raw}):
                with self.assertRaises(mod.SafetyError):
                    mod.bounded('TEST_VALUE', 1, 1, 65535, True)


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / 'run'
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.server = self.root / 'fake-server'
        self.server.write_text('#!' + sys.executable + '\n' + FAKE_SERVER)
        self.server.chmod(0o755)
        self.model = self.root / 'model.gguf'
        self.model.write_bytes(b'NOT A MODEL')
        self.write_mode({})
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('BONSAI_', 'RUNPOD_'))}
        self.env.update(BONSAI_ROOT=str(self.root), BONSAI_STATE_DIR=str(self.state),
                        BONSAI_SERVER_BIN=str(self.server), BONSAI_MODEL_PATH=str(self.model),
                        BONSAI_PORT=str(self.port), BONSAI_READY_TIMEOUT='8',
                        BONSAI_POLL_SECONDS='0.05', BONSAI_PROBE_TIMEOUT='0.1',
                        BONSAI_WATCHDOG_INTERVAL='0.05', BONSAI_RESTART_DELAY='0.05',
                        BONSAI_WATCHDOG_INIT_TIMEOUT='5', RUNPOD_POD_ID='offline-test')
        self.runners = []

    def tearDown(self):
        try:
            env = dict(self.env, BONSAI_WATCHDOG_DRY_RUN='1')
            subprocess.run([sys.executable, str(SCRIPT), 'stop'], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=18)
            # Direct run tests are our children; always reap them.
            for runner in self.runners:
                if runner.poll() is None:
                    runner.terminate()
                runner.wait(timeout=8)
        finally:
            self.tmp.cleanup()

    def write_mode(self, data):
        (self.root / 'fake.json').write_text(json.dumps(data))

    def cmd(self, verb, *args, **changes):
        try:
            return subprocess.run([sys.executable, str(SCRIPT), verb, *args],
                                  env=dict(self.env, **changes), text=True,
                                  capture_output=True, timeout=25)
        except subprocess.TimeoutExpired:
            print("TIMEOUT RECORD:", self.record(), flush=True)
            for name in ("launch.log", "supervisor.log", "server.log", "watchdog.log"):
                path = self.state / name
                if path.exists():
                    print(name, path.read_text()[-2000:], flush=True)
            raise

    def run_background(self):
        process = subprocess.Popen([sys.executable, str(SCRIPT), 'run'], env=self.env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.runners.append(process)
        return process

    def record(self):
        try:
            return json.loads((self.state / 'processes.json').read_text())
        except FileNotFoundError:
            return {}

    def until(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.03)
        self.fail('Condition timed out: ' + json.dumps(self.record()))

    def test_ready_start_is_idempotent_and_stop_is_local(self):
        first = self.cmd('start')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn('DRY-RUN', first.stdout)
        before = self.record()
        second = self.cmd('start')
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(before['supervisor'], self.record()['supervisor'])
        self.assertEqual((self.root / 'events').read_text().count('server-start'), 1)
        result = self.cmd('stop')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('GPU Pod is NOT stopped', result.stdout)
        self.assertTrue(all(not mod.matches(r) for r in self.record()['children'].values()))

    def test_parallel_starts_only_spawn_once(self):
        self.write_mode({'ready_after': 0.3})
        commands = [subprocess.Popen([sys.executable, str(SCRIPT), 'start'], env=self.env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        for cmd in commands:
            out, err = cmd.communicate(timeout=8)
            self.assertEqual(cmd.returncode, 0, out + err)
        self.assertEqual((self.root / 'events').read_text().count('server-start'), 1)

    def test_watchdog_alive_before_model_ready(self):
        self.write_mode({'ready_after': 0.6})
        self.run_background()
        self.until(lambda: self.record().get('phase') == 'loading')
        self.assertTrue(mod.matches(self.record()['children']['watchdog']))
        self.until(lambda: self.record().get('phase') == 'ready')

    def test_model_exit_keeps_watchdog_alive_and_reports_failure(self):
        self.write_mode({'exit': True})
        result = self.cmd('start')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.record()['phase'], 'server_failed')
        self.assertTrue(mod.matches(self.record()['children']['watchdog']))
        self.assertFalse(mod.matches(self.record()['children']['server']))

    def test_timeout_and_invalid_slots_keep_watchdog_not_model(self):
        self.write_mode({'bad_slots': True})
        result = self.cmd('start', BONSAI_READY_TIMEOUT='0.4')
        self.assertNotEqual(result.returncode, 0)
        data = self.record()
        self.assertEqual(data['phase'], 'server_failed')
        self.assertTrue(mod.matches(data['children']['watchdog']))
        self.assertFalse(mod.matches(data['children']['server']))

    def test_old_bare_pid_does_not_suppress_start_or_kill_other_process(self):
        self.state.mkdir(mode=0o700)
        (self.state / 'server.pid').write_text(str(os.getpid()))
        result = self.cmd('start')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.cmd('stop').returncode, 0)
        self.assertTrue(mod.matches(mod.identity(os.getpid())))

    def test_stale_structured_pid_is_not_a_running_supervisor(self):
        self.state.mkdir(mode=0o700)
        record = mod.identity(os.getpid())
        record['start_ticks'] += 1
        mod.save_json(self.state / 'processes.json', {'format': 1, 'supervisor': record, 'children': {}, 'phase': 'ready'})
        result = self.cmd('start')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_corrupt_state_refuses_start_without_spawning(self):
        self.state.mkdir(mode=0o700)
        bad = self.state / 'processes.json'
        bad.write_text('broken')
        bad.chmod(0o600)
        result = self.cmd('start')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'events').exists())

    def test_watchdog_init_failure_prevents_model_start(self):
        broken = self.root / 'broken-watchdog.py'
        broken.write_text('raise SystemExit(6)\n')
        result = self.cmd('start', BONSAI_WATCHDOG=str(broken))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'events').exists())

    def test_watchdog_crash_stops_model_then_restarts_same_session(self):
        result = self.cmd('start')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        before = self.record()
        first = before['children']['watchdog']
        os.kill(first['pid'], signal.SIGKILL)
        self.until(lambda: self.record().get('phase') == 'server_failed'
                   and self.record()['children']['watchdog'] != first)
        after = self.record()
        self.assertTrue(mod.matches(after['children']['watchdog']))
        self.assertFalse(mod.matches(after['children']['server']))
        self.assertEqual(before['session_id'], after['session_id'])
        self.assertEqual(before['session_origin'], after['session_origin'])

    def test_watchdog_restart_budget_is_bounded(self):
        self.env['BONSAI_WATCHDOG_RESTARTS'] = '0'
        result = self.cmd('start')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        os.kill(self.record()['children']['watchdog']['pid'], signal.SIGKILL)
        self.until(lambda: self.record().get('phase') == 'failed')
        self.until(lambda: not any(mod.matches(p) for p in self.record()['children'].values()))

    def test_unmanaged_listener_is_neither_adopted_nor_killed(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', self.port))
            listener.listen()
            result = self.cmd('start')
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.root / 'events').exists())
            self.assertEqual(listener.getsockname()[1], self.port)

    def test_orphans_are_detected_and_stopped_using_fingerprints(self):
        parent = self.run_background()
        self.until(lambda: self.record().get('phase') == 'ready')
        parent.kill()
        parent.wait(timeout=3)
        data = json.loads(self.cmd('status').stdout)
        self.assertEqual(data['phase'], 'orphaned')
        self.assertTrue(data['children_alive']['watchdog'])
        self.assertNotEqual(self.cmd('start').returncode, 0)
        self.assertEqual(self.cmd('stop').returncode, 0)
        self.assertFalse(any(mod.matches(p) for p in self.record()['children'].values()))

    def test_supervisor_restart_reuses_deadline_new_session_is_explicit(self):
        result = self.cmd('start')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first = self.record()
        self.assertEqual(self.cmd('stop').returncode, 0)
        result = self.cmd('start')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        second = self.record()
        self.assertEqual(first['session_id'], second['session_id'])
        self.assertEqual(first['session_origin'], second['session_origin'])
        self.assertEqual(self.cmd('stop').returncode, 0)
        denied = self.cmd('start', '--session-id', 'different')
        self.assertNotEqual(denied.returncode, 0)
        allowed = self.cmd('start', '--session-id', 'different', '--new-session')
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual(self.record()['session_id'], 'different')

    def test_dry_run_deadline_stops_only_local_processes(self):
        self.env.update(BONSAI_MAX_HOURS='0.001')
        self.run_background()
        self.until(lambda: self.record().get('phase') == 'ready')
        self.until(lambda: self.record().get('phase') == 'dry_run_complete')
        self.until(lambda: not any(mod.matches(p) for p in self.record()['children'].values()))

    def test_refuses_real_stop_before_model_launch(self):
        result = self.cmd('start', BONSAI_WATCHDOG_DRY_RUN='0')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'events').exists())

    def test_credentials_not_in_child_environment(self):
        result = self.cmd('start', RUNPOD_API_KEY='FAKE_SECRET_MUST_NOT_PROPAGATE', HF_TOKEN='FAKE_HF')
        self.assertEqual(result.returncode, 0, result.stderr)
        env = json.loads((self.root / 'server-env.json').read_text())
        self.assertNotIn('RUNPOD_API_KEY', env)
        self.assertNotIn('HF_TOKEN', env)

    def test_live_log_limit_is_enforced(self):
        self.write_mode({'noise': 20000})
        result = self.cmd('start', BONSAI_LOG_BYTES='1024')
        self.assertEqual(result.returncode, 0, result.stderr)
        files = list(self.state.glob('server.log*'))
        self.assertLessEqual(len(files), 3)
        self.assertTrue(all(p.stat().st_size <= 1024 for p in files))

    def test_stop_remains_available_when_dry_run_env_changes(self):
        result = self.cmd('start')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.cmd('stop', BONSAI_WATCHDOG_DRY_RUN='0')
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
