"""Offline lifecycle regressions: no paid action, real credential or remote SSH."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("runpod_lifecycle", ROOT / "bin/runpod-lifecycle.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Clock:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def deadline(self, seconds=20):
        return m.Deadline(seconds, self.clock, self.sleep)


def pod(state="RUNNING"):
    return {"id": "test-pod", "status": state,
            "runtime": {"ports": [{"type": "tcp", "private": 22,
                                   "public": 18022, "ip": "192.0.2.10"}]}}


def ready(session="existing-session"):
    return {"phase": "ready", "supervisor_alive": True, "session_id": session,
            "children_alive": {"server": True, "watchdog": True}}


class FakeOps:
    def __init__(self, state="EXITED", remote=None):
        self.state = state
        self.current = {"phase": "absent"} if remote is None else remote
        self.actions = []
        self.starts = []
        self.gets = 0
        self.tunnel = Mock()
        self.tunnel.ready.return_value = True
        self.ssh_probe = Mock(return_value=True)
        self.health = Mock(return_value=True)
        self.preflight = Mock()
        self.action_error = None
        self.mutate = True

    def get(self, deadline):
        deadline.remaining()
        self.gets += 1
        return pod(self.state)

    def action(self, action, deadline):
        deadline.remaining()
        self.actions.append(action)
        if self.mutate:
            self.state = "RUNNING" if action == "start" else "EXITED"
        if self.action_error:
            raise self.action_error

    def remote_status(self, deadline):
        deadline.remaining()
        return self.current

    def remote_start(self, deadline, record, reset):
        self.starts.append((record.copy() if record else None, reset))
        self.current = ready(record["session_id"] if record else "existing-session")


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.cfg = m.client.Config({"RUNPOD_POD_ID": "test-pod", "RUNPOD_API_KEY": "FAKE_SECRET"}, self.home)
        m.client.private_directory(self.cfg.state_dir)
        self.journal = m.Journal(self.cfg)
        self.clock = Clock()
        out = contextlib.redirect_stdout(io.StringIO())
        err = contextlib.redirect_stderr(io.StringIO())
        out.__enter__()
        err.__enter__()
        self.addCleanup(out.__exit__, None, None, None)
        self.addCleanup(err.__exit__, None, None, None)

    def assert_code(self, code, call):
        with self.assertRaises(m.Error) as result:
            call()
        self.assertEqual(result.exception.code, code)


class UpTests(Base):
    def test_stopped_full_chain_and_repeat_idempotent(self):
        ops = FakeOps()
        m.up(ops, self.journal, self.clock.deadline(), yes=True)
        self.assertEqual(ops.actions, ["start"])
        self.assertEqual(len(ops.starts), 1)
        self.assertEqual(self.journal.data["phase"], "ready")
        m.up(ops, m.Journal(self.cfg), self.clock.deadline(), yes=True)
        self.assertEqual(ops.actions, ["start"])
        self.assertEqual(len(ops.starts), 1)

    def test_running_ready_not_restarted_or_session_reset(self):
        ops = FakeOps("RUNNING", ready())
        m.up(ops, self.journal, self.clock.deadline())
        self.assertFalse(ops.actions)
        self.assertFalse(ops.starts)
        self.assertIsNone(self.journal.data)

    def test_running_absent_model_no_paid_start(self):
        ops = FakeOps("RUNNING")
        m.up(ops, self.journal, self.clock.deadline())
        self.assertEqual(ops.starts, [(None, False)])
        self.assertFalse(ops.actions)

    def test_cancel_and_eof_do_not_write_intent_or_post(self):
        for confirm in (lambda _: "no", Mock(side_effect=EOFError)):
            ops = FakeOps()
            m.up(ops, self.journal, self.clock.deadline(), confirm=confirm)
            self.assertFalse(ops.actions)
            self.assertFalse(self.journal.path.exists())

    def test_interactive_yes(self):
        ops = FakeOps()
        confirm = Mock(return_value="yes")
        m.up(ops, self.journal, self.clock.deadline(), confirm=confirm)
        confirm.assert_called_once()
        self.assertEqual(ops.actions, ["start"])

    def test_preflight_failure_before_paid_start(self):
        ops = FakeOps()
        ops.preflight.side_effect = m.Error("manager absent", 2)
        self.assert_code(2, lambda: m.up(ops, self.journal, self.clock.deadline(), yes=True))
        self.assertFalse(ops.actions)

    def test_capacity_no_retry_or_tunnel(self):
        ops = FakeOps()
        ops.mutate = False
        ops.action_error = m.Error("GPU_UNAVAILABLE", 3)
        self.assert_code(3, lambda: m.up(ops, self.journal, self.clock.deadline(), yes=True))
        self.assertEqual(ops.actions, ["start"])
        self.assertEqual(self.journal.data["phase"], "capacity_denied")
        ops.tunnel.start.assert_not_called()

    def test_ambiguous_accepted_start_reconciles_without_second_post(self):
        ops = FakeOps()
        ops.action_error = m.Error("lost response")
        m.up(ops, self.journal, self.clock.deadline(), yes=True)
        self.assertEqual(ops.actions, ["start"])
        self.assertEqual(self.journal.data["phase"], "ready")

    def test_unresolved_start_remains_unresolved_across_retry(self):
        ops = FakeOps()
        ops.mutate = False
        ops.action_error = m.Error("timeout")
        self.assert_code(124, lambda: m.up(ops, self.journal, self.clock.deadline(3), yes=True))
        self.assertEqual(self.journal.data["phase"], "start_requested")
        self.assert_code(1, lambda: m.up(ops, m.Journal(self.cfg), self.clock.deadline(), yes=True))
        self.assertEqual(ops.actions, ["start"])

    def test_known_pending_start_running_resumes(self):
        self.journal.new_intent()
        ops = FakeOps("RUNNING")
        m.up(ops, self.journal, self.clock.deadline(), yes=True)
        self.assertFalse(ops.actions)
        self.assertEqual(ops.starts[0][0]["session_id"], self.journal.data["session_id"])

    def test_unknown_transition_no_action(self):
        ops = FakeOps("STARTING")
        self.assert_code(1, lambda: m.up(ops, self.journal, self.clock.deadline(), yes=True))
        self.assertFalse(ops.actions)

    def test_ssh_deadline_preserves_pod_no_rollback(self):
        ops = FakeOps()
        ops.ssh_probe.return_value = False
        self.assert_code(124, lambda: m.up(ops, self.journal, self.clock.deadline(3), yes=True))
        self.assertEqual(ops.actions, ["start"])
        self.assertFalse(ops.starts)

    def test_loading_supervisor_not_started_twice(self):
        ops = FakeOps("RUNNING", {"phase": "loading", "supervisor_alive": True})
        ops.remote_status = Mock(side_effect=[ops.current, ready(), ready()])
        m.up(ops, self.journal, self.clock.deadline())
        self.assertFalse(ops.starts)

    def test_failed_active_remote_never_killed_or_restarted(self):
        ops = FakeOps("RUNNING", {"phase": "server_failed", "supervisor_alive": True})
        self.assert_code(1, lambda: m.up(ops, self.journal, self.clock.deadline()))
        self.assertFalse(ops.starts)
        self.assertFalse(ops.actions)

    def test_expired_same_session_not_reset(self):
        self.journal.new_intent()
        self.journal.phase("ready")
        ops = FakeOps("RUNNING", {"phase": "dry_run_complete", "supervisor_alive": False,
                                   "session_id": self.journal.data["session_id"]})
        self.assert_code(1, lambda: m.up(ops, self.journal, self.clock.deadline()))
        self.assertFalse(ops.starts)

    def test_intentional_new_paid_run_can_replace_inactive_old_session(self):
        ops = FakeOps("EXITED", {"phase": "dry_run_complete", "supervisor_alive": False,
                                  "session_id": "old-session"})
        m.up(ops, self.journal, self.clock.deadline(), yes=True)
        self.assertTrue(ops.starts[0][1])
        self.assertNotEqual(self.journal.data["session_id"], "old-session")

    def test_orphan_refused_even_after_new_paid_run(self):
        ops = FakeOps("EXITED", {"phase": "orphaned", "supervisor_alive": False})
        self.assert_code(1, lambda: m.up(ops, self.journal, self.clock.deadline(), yes=True))
        self.assertFalse(ops.starts)
        self.assertEqual(ops.actions, ["start"])

    def test_existing_different_active_session_not_adopted(self):
        self.journal.new_intent()
        self.journal.phase("running")
        ops = FakeOps("RUNNING", ready("someone-else"))
        self.assert_code(1, lambda: m.up(ops, self.journal, self.clock.deadline()))
        self.assertFalse(ops.starts)

    def test_health_failure_no_cloud_rollback_no_duplicate_remote_start_on_retry(self):
        ops = FakeOps()
        ops.health.return_value = False
        self.assert_code(124, lambda: m.up(ops, self.journal, self.clock.deadline(2), yes=True))
        ops.health.return_value = True
        m.up(ops, m.Journal(self.cfg), self.clock.deadline(), yes=True)
        self.assertEqual(ops.actions, ["start"])
        self.assertEqual(len(ops.starts), 1)

    def test_tunnel_failure_preserves_existing_running_pod(self):
        ops = FakeOps("RUNNING", ready())
        ops.tunnel.start.side_effect = m.Error("unit failed")
        self.assert_code(1, lambda: m.up(ops, self.journal, self.clock.deadline()))
        self.assertFalse(ops.actions)

    def test_final_health_alone_is_not_success(self):
        ops = FakeOps("RUNNING", ready())
        ops.remote_status = Mock(side_effect=[ready(), ready(), {"phase": "server_failed"}])
        self.assert_code(1, lambda: m.up(ops, self.journal, self.clock.deadline()))

    def test_old_stopped_journal_not_relabelled_ready_for_untracked_run(self):
        self.journal.new_intent()
        self.journal.phase("stopped")
        ops = FakeOps("RUNNING", ready())
        m.up(ops, self.journal, self.clock.deadline())
        self.assertEqual(self.journal.data["phase"], "stopped")
        m.up(ops, m.Journal(self.cfg), self.clock.deadline())


class DownWaitTests(Base):
    def test_down_stops_tunnel_and_confirms_exited(self):
        ops = FakeOps("RUNNING")
        self.journal.new_intent()
        m.down(ops, self.journal, self.clock.deadline())
        ops.tunnel.stop.assert_called_once()
        self.assertEqual(ops.actions, ["stop"])
        self.assertEqual(self.journal.data["phase"], "stopped")

    def test_tunnel_failure_never_blocks_requested_cloud_stop(self):
        ops = FakeOps("RUNNING")
        ops.tunnel.stop.side_effect = m.Error("foreign unit")
        self.assert_code(1, lambda: m.down(ops, self.journal, self.clock.deadline()))
        self.assertEqual(ops.actions, ["stop"])
        self.assertEqual(ops.state, "EXITED")

    def test_down_already_exited_is_idempotent(self):
        ops = FakeOps()
        m.down(ops, self.journal, self.clock.deadline())
        m.down(ops, self.journal, self.clock.deadline())
        self.assertFalse(ops.actions)

    def test_stop_timeout_after_acceptance_reconciles(self):
        ops = FakeOps("RUNNING")
        ops.action_error = m.Error("lost response")
        m.down(ops, self.journal, self.clock.deadline())
        self.assertEqual(ops.actions, ["stop"])

    def test_rejected_stop_does_not_claim_exited_or_repeat_post(self):
        ops = FakeOps("RUNNING")
        ops.mutate = False
        ops.action_error = m.Error("HTTP403")
        self.assert_code(124, lambda: m.down(ops, self.journal, self.clock.deadline(3)))
        self.assertEqual(ops.actions, ["stop"])

    def test_down_can_proceed_without_valid_journal(self):
        ops = FakeOps("RUNNING")
        m.down(ops, None, self.clock.deadline())
        self.assertEqual(ops.actions, ["stop"])

    def test_wait_only_reads(self):
        ops = FakeOps("RUNNING")
        m.wait_state(ops, "RUNNING", self.clock.deadline())
        self.assertFalse(ops.actions)
        ops.tunnel.start.assert_not_called()
        self.assertFalse(ops.starts)

    def test_wait_timeout_and_read_errors_no_actions(self):
        ops = FakeOps()
        self.assert_code(124, lambda: m.wait_state(ops, "RUNNING", self.clock.deadline(3)))
        self.assertFalse(ops.actions)
        ops.get = Mock(side_effect=m.Error("wrong pod"))
        self.assert_code(1, lambda: m.wait_state(ops, "RUNNING", self.clock.deadline()))


class StateTests(Base):
    def test_private_record_no_secret_and_reloads(self):
        self.journal.new_intent(now=100)
        data = self.journal.path.read_text()
        self.assertNotIn("FAKE_SECRET", data)
        self.assertEqual(self.journal.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(m.Journal(self.cfg).data, self.journal.data)

    def test_corrupt_wrong_pod_public_and_symlink_records_rejected(self):
        self.journal.new_intent()
        good = self.journal.path.read_text()
        for payload in ("broken", "[]", good.replace("test-pod", "other"), good.replace("start_requested", "bad")):
            self.journal.path.write_text(payload)
            self.assert_code(2, lambda: m.Journal(self.cfg))
        self.journal.path.write_text(good)
        self.journal.path.chmod(0o644)
        self.assert_code(2, lambda: m.Journal(self.cfg))
        self.journal.path.unlink()
        self.journal.path.symlink_to(self.home / "other.json")
        self.assert_code(2, lambda: m.Journal(self.cfg))

    def test_lock_is_exclusive_and_released(self):
        with m.lifecycle_lock(self.cfg):
            with self.assertRaises(m.Error):
                with m.lifecycle_lock(self.cfg):
                    pass
        with m.lifecycle_lock(self.cfg):
            pass

    def test_deadline_rejects_nonfinite_and_zero(self):
        for value in (0, -1, float("nan"), float("inf")):
            self.assert_code(2, lambda: self.clock.deadline(value))

    def test_false_ready_requires_children_and_supervisor(self):
        self.assertTrue(m.remote_ready(ready()))
        for field in ("supervisor_alive", "children_alive"):
            data = ready()
            data.pop(field)
            self.assertFalse(m.remote_ready(data))


class SystemdTests(Base):
    def setUp(self):
        super().setUp()
        self.run = Mock()
        self.tunnel = m.UserTunnel(self.cfg, self.home / "config.env", self.run)

    def response(self, active="active", description=None, transient="yes"):
        text = (f"LoadState=loaded\nActiveState={active}\nSubState=running\n"
                f"Description={description or self.tunnel.description}\nTransient={transient}\nMainPID=2345\n")
        return subprocess.CompletedProcess([], 0, text.encode(), b"")

    def missing(self):
        return subprocess.CompletedProcess([], 4, b"LoadState=not-found\n", b"")

    def test_foreign_unit_never_stopped(self):
        self.run.return_value = self.response(description="other")
        self.assert_code(2, lambda: self.tunnel.stop(self.clock.deadline()))
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(self.run.call_args.args[0][2], "show")

    def test_transient_start_honors_config_and_no_secret_argv(self):
        self.run.side_effect = [self.missing(), subprocess.CompletedProcess([], 0, b"", b"")]
        with patch.object(m, "local_port_free", return_value=True):
            self.assertTrue(self.tunnel.start(self.clock.deadline()))
        argv = self.run.call_args.args[0]
        self.assertEqual(argv[0], "systemd-run")
        self.assertIn("--property=KillMode=control-group", argv)
        self.assertIn("--collect", argv)
        self.assertIn("--setenv=RUNPOD_BONSAI_CONFIG=" + str(self.home / "config.env"), argv)
        self.assertNotIn("FAKE_SECRET", " ".join(argv))

    def test_active_owned_tunnel_is_reused(self):
        self.run.return_value = self.response()
        self.assertFalse(self.tunnel.start(self.clock.deadline()))
        self.assertEqual(self.run.call_count, 1)

    def test_missing_stop_noop(self):
        self.run.return_value = self.missing()
        self.tunnel.stop(self.clock.deadline())
        self.assertEqual(self.run.call_count, 1)

    def test_manager_error_is_not_missing_service(self):
        self.run.return_value = subprocess.CompletedProcess([], 1, b"", b"bus unavailable")
        self.assert_code(2, lambda: self.tunnel.properties(self.clock.deadline()))

    def test_unmanaged_port_not_adopted(self):
        self.run.return_value = self.missing()
        with patch.object(m, "local_port_free", return_value=False):
            self.assert_code(1, lambda: self.tunnel.start(self.clock.deadline()))
        self.assertEqual(self.run.call_count, 1)

    def test_healthy_port_without_owned_ssh_not_ready(self):
        self.run.return_value = self.response()
        with patch.object(m, "ssh_owned_listener", return_value=False):
            self.assertFalse(self.tunnel.ready(self.clock.deadline()))

    def test_config_identity_changes_for_ports_not_api_key(self):
        original = self.tunnel.description
        self.cfg.api_key = "OTHER_FAKE_SECRET"
        self.assertEqual(m.UserTunnel(self.cfg, self.home / "config.env").description, original)
        self.cfg.remote_port += 1
        self.assertNotEqual(m.UserTunnel(self.cfg, self.home / "config.env").description, original)

    def test_missing_systemd_tool_fails_preflight(self):
        with patch.object(m.shutil, "which", return_value=None):
            self.assert_code(2, lambda: self.tunnel.preflight(self.clock.deadline()))
        self.run.assert_not_called()

    def test_stop_accepted_waits_until_inactive(self):
        self.run.side_effect = [self.response(), subprocess.CompletedProcess([], 0, b"", b""),
                                self.response("inactive")]
        self.tunnel.stop(self.clock.deadline())
        self.assertEqual(self.run.call_count, 3)


class AdapterTests(Base):
    def test_remote_root_port_and_literal_quoting(self):
        self.cfg.remote_root = "/workspace/a project's files"
        self.cfg.remote_port = 19080
        ops = m.Operations(self.cfg, self.home / "config")
        command = ops.pod_command("status")
        self.assertIn("BONSAI_PORT=19080", command)
        self.assertIn("BONSAI_ROOT=/workspace/a project's files", command)
        self.assertIn("BONSAI_WATCHDOG_DRY_RUN=1", command)
        self.assertIn("BONSAI_STATE_DIR=/workspace/ai/Bonsai-demo/run", command)
        args = m.client.ssh_arguments(self.cfg, ("192.0.2.1", 22), command)
        import shlex
        self.assertEqual(shlex.split(args[-1]), command)

    def test_remote_state_dir_custom_and_validation(self):
        config = m.client.Config({"RUNPOD_POD_ID": "test-pod", "RUNPOD_API_KEY": "FAKE_SECRET",
                                  "BONSAI_REMOTE_STATE_DIR": "/root/bonsai-state"}, self.home)
        ops = m.Operations(config, self.home / "config")
        self.assertIn("BONSAI_STATE_DIR=/root/bonsai-state", ops.pod_command("status"))
        self.assertIn("BONSAI_STATE_DIR=/root/bonsai-state", ops.pod_command("start"))
        with self.assertRaises(m.client.ClientError):
            m.client.Config({"RUNPOD_POD_ID": "test-pod", "RUNPOD_API_KEY": "FAKE_SECRET",
                             "BONSAI_REMOTE_STATE_DIR": "/root/../bad"}, self.home)

    def test_subprocess_credentials_filtered(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, b"", b""))
        with patch.dict(os.environ, {"RUNPOD_API_KEY": "SECRET", "BONSAI_TOKEN": "SECRET"}):
            m.run_command(["fake-command"], 2, runner)
        env = runner.call_args.kwargs["env"]
        self.assertNotIn("RUNPOD_API_KEY", env)
        self.assertNotIn("BONSAI_TOKEN", env)

    def test_command_timeout_redacted(self):
        runner = Mock(side_effect=subprocess.TimeoutExpired(["SECRET"], 2))
        self.assert_code(124, lambda: m.run_command(["fake"], 2, runner))

    def test_remote_status_rejects_wrong_shape_and_failure(self):
        ops = m.Operations(self.cfg, self.home / "config")
        for code, text in ((0, b"[]"), (0, b"not-json"), (1, b"PRIVATE OUTPUT")):
            with patch.object(ops, "remote", return_value=subprocess.CompletedProcess([], code, text, b"")):
                self.assert_code(1, lambda: ops.remote_status(self.clock.deadline()))

    def test_api_time_budget_restored(self):
        ops = m.Operations(self.cfg, self.home / "config")
        original = self.cfg.api_timeout
        def get():
            self.assertLessEqual(self.cfg.api_timeout, 2)
            return pod()
        ops.api = Mock(get=get)
        ops.get(self.clock.deadline(2))
        self.assertEqual(self.cfg.api_timeout, original)

    def test_actual_loopback_listener_owned_only_by_child(self):
        script = "import socket,time; s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); print(s.getsockname()[1],flush=True); time.sleep(10)"
        process = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            port = int(process.stdout.readline())
            self.assertTrue(m.ssh_owned_listener(os.getpid(), port))
            self.assertFalse(m.ssh_owned_listener(1, port))
            self.assertFalse(m.local_port_free(port))
        finally:
            process.terminate()
            process.communicate(timeout=5)

    def test_wrapper_help_and_invalid_flags_no_configuration(self):
        for args in (("up", "--help"), ("wait", "--help"), ("down", "--help")):
            result = subprocess.run(["bash", str(ROOT / "bin/runpod-bonsai"), *args], capture_output=True,
                                    env={**os.environ, "RUNPOD_BONSAI_CONFIG": "/absent"})
            self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run(["bash", str(ROOT / "bin/runpod-bonsai"), "up", "--bad-flag"], capture_output=True)
        self.assertEqual(result.returncode, 2)

    def test_malformed_remote_liveness_fields_are_safe_errors(self):
        ops = m.Operations(self.cfg, self.home / "config")
        for invalid in ({"phase": "invented"}, {"phase": "ready", "children_alive": None},
                        {**ready(), "supervisor_alive": "true"},
                        {**ready(), "children_alive": {"server": 1}},
                        {**ready(), "session_id": "bad\nvalue"}):
            with patch.object(ops, "remote", return_value=subprocess.CompletedProcess([], 0, json.dumps(invalid).encode(), b"")):
                self.assert_code(1, lambda: ops.remote_status(self.clock.deadline()))

    def test_remote_start_arguments_preserve_session_and_origin(self):
        ops = m.Operations(self.cfg, self.home / "config")
        ops.remote = Mock(return_value=subprocess.CompletedProcess([], 0, b"", b""))
        ops.remote_start(self.clock.deadline(), {"session_id": "run-test", "started_at": 100.5}, True)
        command = ops.remote.call_args.args[0]
        self.assertIn("RUNPOD_POD_ID=test-pod", command)
        self.assertIn("--new-session", command)
        self.assertEqual(command[command.index("--session-id") + 1], "run-test")
        self.assertEqual(command[command.index("--session-start-epoch") + 1], "100.5")

    def test_down_main_ignores_corrupt_journal_for_cloud_stop(self):
        ops = FakeOps("RUNNING")
        with patch.object(m.client.Config, "load", return_value=self.cfg), \
                patch.object(m, "Operations", return_value=ops), \
                patch.object(m, "Journal", side_effect=m.Error("corrupt", 2)):
            self.assertEqual(m.main(["down"]), 0)
        self.assertEqual(ops.actions, ["stop"])

    def test_invalid_timeout_before_api_and_config(self):
        with patch.object(m.client.Config, "load") as load:
            for value in ("0", "-1", "inf", "3601"):
                with self.assertRaises(SystemExit) as exc:
                    m.main(["wait", "--timeout", value])
                self.assertEqual(exc.exception.code, 2)
            load.assert_not_called()

    def test_cli_nan_rejected_before_config(self):
        result = subprocess.run([sys.executable, str(ROOT / "bin/runpod-lifecycle.py"), "up", "--timeout", "nan"], capture_output=True)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
