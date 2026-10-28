"""Offline controller/config/SSH regression tests. Never contact RunPod or real SSH."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("runpod_client", ROOT / "bin/runpod-client.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Response:
    def __init__(self, data=None, raw=None, status=200):
        self.raw = raw if raw is not None else json.dumps(data).encode()
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False

    def read(self, length=-1):
        return self.raw if length == -1 else self.raw[:length]


def pod(ip="192.0.2.40", port=22222, status="RUNNING"):
    return {"id": "test-pod", "name": "synthetic", "status": status,
            "runtime": {"ports": [{"type": "tcp", "private": 22, "public": port, "ip": ip}]}}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.values = {"RUNPOD_POD_ID": "test-pod", "RUNPOD_API_KEY": "secret-not-for-logs"}
        self.config = m.Config(self.values, self.home)

    def config_file(self, extra="", mode=0o600):
        path = self.home / "runpod.env"
        path.write_text('RUNPOD_POD_ID=test-pod\nRUNPOD_API_KEY="secret-not-for-logs"\n' + extra)
        path.chmod(mode)
        return path

    def setup_ssh(self):
        self.config.key.parent.mkdir(mode=0o700)
        self.config.key.write_text("not-a-real-key")
        self.config.key.chmod(0o600)


class ConfigTests(Fixture):
    def test_defaults(self):
        self.assertEqual(self.config.local_port, 18082)
        self.assertEqual(self.config.key, self.home / ".ssh/runpod_ed25519")

    def test_owned_private_config_loads(self):
        path = self.config_file()
        cfg = m.Config.load({"HOME": str(self.home), "RUNPOD_BONSAI_CONFIG": str(path)})
        self.assertEqual(cfg.pod_id, "test-pod")

    def test_public_config_rejected(self):
        path = self.config_file(mode=0o644)
        with self.assertRaises(m.ClientError):
            m.Config.load({"HOME": str(self.home), "RUNPOD_BONSAI_CONFIG": str(path)})

    def test_symlink_config_rejected(self):
        path = self.config_file()
        link = self.home / "linked.env"
        link.symlink_to(path)
        with self.assertRaises(m.ClientError):
            m.private_file(link)

    def test_wrong_owner_rejected(self):
        path = self.config_file()
        with patch.object(m.os, "geteuid", return_value=999999):
            with self.assertRaises(m.ClientError):
                m.private_file(path)

    def test_config_is_data_not_shell(self):
        marker = self.home / "executed"
        cfg = m.parse_config(f"RUNPOD_API_KEY='$(touch {marker})'\n")
        self.assertIn("$(touch", cfg["RUNPOD_API_KEY"])
        self.assertFalse(marker.exists())
        with self.assertRaises(m.ClientError):
            m.Config(self.values | cfg, self.home)

    def test_home_and_quotes(self):
        values = m.parse_config('export RUNPOD_SSH_KEY="$HOME/.ssh/a key" # comment\n'
                                "BONSAI_REMOTE_ROOT='/workspace/Bonsai project'\n")
        cfg = m.Config(self.values | values, self.home)
        self.assertEqual(str(cfg.key), str(self.home / ".ssh/a key"))
        self.assertEqual(cfg.remote_root, "/workspace/Bonsai project")

    def test_no_duplicate_or_legacy_keys(self):
        for text in ("RUNPOD_PORT=22", "RUNPOD_HOST=example", "RUNPOD_POD_ID=a\nRUNPOD_POD_ID=b"):
            with self.subTest(text=text), self.assertRaises(m.ClientError):
                m.parse_config(text)

    def test_invalid_ports(self):
        for value in ("0", "65536", "-1", "2.5", "NaN", "1;echo", "999999999999", True):
            with self.subTest(value=value), self.assertRaises(m.ClientError):
                m.integer(value, "port")

    def test_leading_zero_port_normalized(self):
        self.assertEqual(m.integer("018082", "port"), 18082)

    def test_invalid_remote_root(self):
        for root in ("/", "relative", "/workspace/../home", "/workspace//a", "/a\nwhoami"):
            with self.subTest(root=root), self.assertRaises(m.ClientError):
                m.Config(self.values | {"BONSAI_REMOTE_ROOT": root}, self.home)

    def test_host_check_cannot_be_disabled(self):
        with self.assertRaises(m.ClientError):
            m.Config(self.values | {"RUNPOD_SSH_HOST_KEY_POLICY": "no"}, self.home)

    def test_ssh_user_option_injection_rejected(self):
        with self.assertRaises(m.ClientError):
            m.Config(self.values | {"RUNPOD_SSH_USER": "-oProxyCommand=id"}, self.home)

    def test_control_characters_rejected(self):
        with self.assertRaises(m.ClientError):
            m.parse_config('RUNPOD_API_KEY="secret\x1b"')

    def test_check_config_no_network_or_secret_output(self):
        path = self.config_file()
        env = os.environ | {"HOME": str(self.home), "RUNPOD_BONSAI_CONFIG": str(path)}
        output = subprocess.run([str(ROOT / "bin/runpod-bonsai"), "check-config"], env=env,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertNotIn(self.config.api_key, output.stdout + output.stderr)


class APITests(Fixture):
    def test_get_identity_and_fixed_url(self):
        opener = Mock(return_value=Response(pod()))
        data = m.RunPodClient(self.config, opener).get()
        req = opener.call_args.args[0]
        self.assertEqual(req.full_url, "https://api.runpod.io/v2/pods/test-pod")
        self.assertEqual(req.method, "GET")
        self.assertEqual(data["status"], "RUNNING")

    def test_wrong_pod_identity_rejected(self):
        with self.assertRaises(m.ClientError):
            m.RunPodClient(self.config, Mock(return_value=Response(pod() | {"id": "other"}))).get()

    def test_malformed_and_oversized_payloads(self):
        for raw in (b"not json", b"x" * (m.BODY_LIMIT + 1), b"[]", b"{}"):
            with self.subTest(raw=raw[:20]), self.assertRaises(m.ClientError):
                m.RunPodClient(self.config, Mock(return_value=Response(raw=raw))).get()

    def test_post_body_and_empty_success(self):
        opener = Mock(return_value=Response(raw=b"", status=204))
        m.RunPodClient(self.config, opener).action("stop")
        req = opener.call_args.args[0]
        self.assertEqual(json.loads(req.data), {"action": "stop"})
        self.assertTrue(req.full_url.endswith("/action"))
        self.assertEqual(req.method, "POST")

    def test_capacity_exit_three_no_retry(self):
        error = urllib.error.HTTPError("https://unused", 400, "bad", {}, io.BytesIO(
            b'{"detail":"There are not enough free GPUs on the host machine to start this pod."}'))
        opener = Mock(side_effect=error)
        with self.assertRaises(m.ClientError) as raised:
            m.RunPodClient(self.config, opener).action("start")
        self.assertEqual(raised.exception.code, 3)
        self.assertIn("GPU_UNAVAILABLE", str(raised.exception))
        self.assertIn("No additional Pod was created", str(raised.exception))
        self.assertEqual(opener.call_count, 1)

    def test_http_failures_redacted(self):
        for status in (400, 401, 403, 429, 500):
            error = urllib.error.HTTPError("https://unused", status, "bad", {}, io.BytesIO(
                json.dumps({"detail": self.config.api_key}).encode()))
            opener = Mock(side_effect=error)
            with self.subTest(status=status), self.assertRaises(m.ClientError) as raised:
                m.RunPodClient(self.config, opener).action("start")
            self.assertNotIn(self.config.api_key, str(raised.exception))
            self.assertEqual(opener.call_count, 1)

    def test_ambiguous_timeout_does_not_retry(self):
        opener = Mock(side_effect=TimeoutError("private"))
        with self.assertRaises(m.ClientError) as raised:
            m.RunPodClient(self.config, opener).action("start")
        self.assertIn("unknown", str(raised.exception))
        self.assertEqual(opener.call_count, 1)

    def test_invalid_actions_rejected(self):
        opener = Mock()
        with self.assertRaises(m.ClientError):
            m.RunPodClient(self.config, opener).action("create")
        opener.assert_not_called()

    def test_confirmation_required_and_eof_cancels(self):
        for confirm in (Mock(return_value="no"), Mock(side_effect=EOFError)):
            api = Mock()
            api.get.return_value = pod(status="EXITED")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(m.controller("start", False, self.config, api, confirm), 0)
            api.action.assert_not_called()

    def test_yes_single_action(self):
        api = Mock()
        api.get.return_value = pod(status="EXITED")
        confirm = Mock(side_effect=AssertionError("prompt must not run"))
        with contextlib.redirect_stdout(io.StringIO()):
            m.controller("start", True, self.config, api, confirm)
        api.action.assert_called_once_with("start")

    def test_interactive_yes(self):
        api = Mock()
        api.get.return_value = pod(status="EXITED")
        with contextlib.redirect_stdout(io.StringIO()):
            m.controller("start", False, self.config, api, lambda prompt: "yes")
        api.action.assert_called_once_with("start")

    def test_start_running_and_stop_exited_are_noops(self):
        for command, state in (("start", "RUNNING"), ("stop", "EXITED")):
            api = Mock()
            api.get.return_value = pod(status=state)
            with contextlib.redirect_stdout(io.StringIO()):
                m.controller(command, True, self.config, api)
            api.action.assert_not_called()

    def test_start_transitional_rejected(self):
        api = Mock()
        api.get.return_value = pod(status="STARTING")
        with self.assertRaises(m.ClientError):
            m.controller("start", True, self.config, api)
        api.action.assert_not_called()

    def test_status_honors_port(self):
        cfg = m.Config(self.values | {"RUNPOD_LOCAL_PORT": "19082"}, self.home)
        api = Mock()
        api.get.return_value = pod()
        with patch.object(m, "health_ready", return_value=True) as health, contextlib.redirect_stdout(io.StringIO()):
            m.controller("status", False, cfg, api)
        health.assert_called_once_with(19082)

    def test_false_health_is_not_ready(self):
        self.assertFalse(m.health_ready(1, Mock(return_value=Response({"status": "loading"}))))
        self.assertFalse(m.health_ready(1, Mock(return_value=Response(raw=b"OK"))))
        self.assertTrue(m.health_ready(19082, Mock(return_value=Response({"status": "ok"}))))

    def test_inspect_does_not_print_env_or_launch_args(self):
        api = Mock()
        api.get.return_value = pod() | {"env": {"KEY": "do-not-print"}, "args": "password=do-not-print"}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            m.inspect_pod(self.config, api)
        self.assertNotIn("do-not-print", out.getvalue())
        self.assertTrue(json.loads(out.getvalue())["custom_launch_args_present"])

    def test_redirects_not_followed(self):
        self.assertIsNone(m.NoRedirect().redirect_request(None, None, None, None, None, "https://evil"))


class EndpointSSHTests(Fixture):
    def test_ipv4_ipv6(self):
        self.assertEqual(m.ssh_endpoint(pod()), ("192.0.2.40", 22222))
        self.assertEqual(m.ssh_endpoint(pod("2001:db8::40")), ("2001:db8::40", 22222))

    def test_stopped_mapping_not_used(self):
        with self.assertRaises(m.ClientError) as raised:
            m.ssh_endpoint(pod(status="EXITED"))
        self.assertEqual(raised.exception.code, 4)

    def test_missing_duplicate_mappings(self):
        for ports in ([], [pod()["runtime"]["ports"][0]] * 2):
            item = pod()
            item["runtime"]["ports"] = ports
            with self.assertRaises(m.ClientError):
                m.ssh_endpoint(item)

    def test_unsafe_ips(self):
        for host in ("127.0.0.1", "0.0.0.0", "::", "::1", "fe80::1", "224.0.0.1",
                     "example.org", "-oProxyCommand=id", "192.0.2.1;id", "2001:db8::1%eth0"):
            with self.subTest(host=host), self.assertRaises(m.ClientError):
                m.ssh_endpoint(pod(host))

    def test_noninteger_or_invalid_port(self):
        for port in (True, 22.5, "22", 0, 65536):
            with self.subTest(port=port), self.assertRaises(m.ClientError):
                m.ssh_endpoint(pod(port=port))

    def test_ssh_options_no_password_no_multiplex(self):
        args = m.ssh_arguments(self.config, m.ssh_endpoint(pod()), ["true"])
        for expected in ("BatchMode=yes", "IdentitiesOnly=yes", "PasswordAuthentication=no",
                         "StrictHostKeyChecking=accept-new", "HostKeyAlias=runpod-test-pod", "ControlMaster=no"):
            self.assertIn(expected, args)
        self.assertNotIn(self.config.api_key, " ".join(args))

    def test_remote_command_quoted(self):
        words = ["printf", "%s", "a;touch /tmp/not-executed", "space and 'quote'", "$(id)"]
        command = m.ssh_arguments(self.config, m.ssh_endpoint(pod()), words)[-1]
        self.assertEqual(shlex.split(command), words)

    def test_remote_root_and_port_propagation(self):
        cfg = m.Config(self.values | {"BONSAI_REMOTE_ROOT": "/workspace/a 'quoted' project",
                                      "RUNPOD_REMOTE_PORT": "19080"}, self.home)
        cmd = m.serve_command(cfg)
        self.assertEqual(cmd[-1], "/workspace/a 'quoted' project/bin/bonsai-server-start")
        self.assertIn("BONSAI_PORT=19080", cmd)
        args = m.ssh_arguments(cfg, m.ssh_endpoint(pod()), cmd)
        self.assertEqual(shlex.split(args[-1]), cmd)

    @unittest.skipUnless(shutil.which("ssh"), "OpenSSH client not installed locally")
    def test_real_openssh_config_parsing_offline(self):
        cfg = m.Config(self.values | {"RUNPOD_KNOWN_HOSTS": str(self.home / ".ssh/known hosts")}, self.home)
        args = m.ssh_arguments(cfg, m.ssh_endpoint(pod()), ["true"])
        result = subprocess.run([args[0], "-G", *args[1:]], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("hostkeyalias runpod-test-pod", result.stdout)
        self.assertIn("batchmode yes", result.stdout)
        self.assertIn("userknownhostsfile " + str(cfg.known_hosts), result.stdout)

    def test_fake_ssh_process_gets_literal_command_and_no_api_key(self):
        self.setup_ssh()
        executables = self.home / "fake-bin"
        executables.mkdir()
        log = self.home / "ssh.json"
        script = executables / "ssh"
        script.write_text("#!" + sys.executable + "\nimport os,sys,json\n"
                          "json.dump({'args':sys.argv[1:],'secret_present': 'RUNPOD_API_KEY' in os.environ}, "
                          "open(os.environ['TEST_SSH_LOG'],'w'))\n")
        script.chmod(0o700)
        api = Mock()
        api.get.return_value = pod()
        with patch.dict(os.environ, {"PATH": str(executables) + os.pathsep + os.environ["PATH"],
                                     "TEST_SSH_LOG": str(log), "RUNPOD_API_KEY": "never-forward"}):
            self.assertEqual(m.run_remote(self.config, api, ["echo", "literal;not-a-command"]), 0)
        data = json.loads(log.read_text())
        self.assertFalse(data["secret_present"])
        self.assertEqual(shlex.split(data["args"][-1]), ["echo", "literal;not-a-command"])

    def test_forward_loopback_and_ports(self):
        cfg = m.Config(self.values | {"RUNPOD_LOCAL_PORT": "19082", "RUNPOD_REMOTE_PORT": "19080"}, self.home)
        args = m.ssh_arguments(cfg, m.ssh_endpoint(pod()), forward=True)
        self.assertEqual(args[args.index("-L") + 1], "127.0.0.1:19082:127.0.0.1:19080")
        self.assertIn("-N", args)

    def test_child_environment_filters_controller_secrets(self):
        with patch.dict(os.environ, {"RUNPOD_API_KEY": "secret", "BONSAI_SECRET": "secret", "PATH": "/usr/bin"}):
            env = m.child_environment()
        self.assertNotIn("RUNPOD_API_KEY", env)
        self.assertNotIn("BONSAI_SECRET", env)
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_known_hosts_created_private_and_never_cleared(self):
        self.setup_ssh()
        m.prepare_ssh(self.config)
        self.assertEqual(stat.S_IMODE(self.config.known_hosts.stat().st_mode), 0o600)
        self.config.known_hosts.write_text("existing host fingerprint\n")
        m.prepare_ssh(self.config)
        self.assertEqual(self.config.known_hosts.read_text(), "existing host fingerprint\n")

    def test_known_hosts_symlink_rejected(self):
        self.setup_ssh()
        self.config.known_hosts.symlink_to(self.config.key)
        with self.assertRaises(m.ClientError):
            m.prepare_ssh(self.config)

    def test_key_permissions_rejected(self):
        self.setup_ssh()
        self.config.key.chmod(0o644)
        with self.assertRaises(m.ClientError):
            m.prepare_ssh(self.config)

    def test_serve_uses_same_builder_and_sanitized_environment(self):
        self.setup_ssh()
        api = Mock()
        api.get.return_value = pod()
        with patch.object(m.subprocess, "run", return_value=Mock(returncode=0)) as runner, \
                patch.dict(os.environ, {"RUNPOD_API_KEY": "secret"}):
            code = m.run_remote(self.config, api, m.serve_command(self.config))
        self.assertEqual(code, 0)
        self.assertNotIn("RUNPOD_API_KEY", runner.call_args.kwargs["env"])
        self.assertEqual(shlex.split(runner.call_args.args[0][-1]), m.serve_command(self.config))


class FakeClock:
    def __init__(self, until=5):
        self.time = 0
        self.until = until

    def clock(self):
        return self.time

    def is_set(self):
        return self.time >= self.until

    def wait(self, delay):
        self.time += delay
        return self.is_set()


class FakeProcess:
    def __init__(self, failed=False):
        self.code = 255 if failed else None
        self.terminated = False

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = -signal.SIGTERM

    def kill(self):
        self.code = -signal.SIGKILL


class TunnelTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config = m.Config(self.values | {"RUNPOD_TUNNEL_REFRESH_SECONDS": "1",
                                             "RUNPOD_TUNNEL_RETRY_SECONDS": "1",
                                             "RUNPOD_TUNNEL_MAX_FAILURES": "3"}, self.home)

    def test_mapping_change_refreshes_while_connected(self):
        api = Mock()
        api.get.side_effect = [pod(), pod("192.0.2.50", 33333), pod(status="EXITED")]
        processes = [FakeProcess(), FakeProcess()]
        spawn = Mock(side_effect=processes)
        clock = FakeClock()
        with contextlib.redirect_stderr(io.StringIO()):
            result = m.tunnel_loop(self.config, api, clock, spawn, clock.clock)
        self.assertEqual(result, 0)
        self.assertEqual(spawn.call_count, 2)
        self.assertIn("root@192.0.2.40", spawn.call_args_list[0].args[0])
        self.assertIn("root@192.0.2.50", spawn.call_args_list[1].args[0])
        for p in processes:
            self.assertTrue(p.terminated)
        api.action.assert_not_called()

    def test_ssh_failure_rereads_mapping(self):
        api = Mock()
        api.get.side_effect = [pod(), pod("192.0.2.60"), pod(status="EXITED")]
        spawn = Mock(side_effect=[FakeProcess(failed=True), FakeProcess()])
        clock = FakeClock()
        with contextlib.redirect_stderr(io.StringIO()):
            m.tunnel_loop(self.config, api, clock, spawn, clock.clock)
        self.assertEqual(spawn.call_count, 2)
        self.assertIn("root@192.0.2.60", spawn.call_args_list[1].args[0])

    def test_retry_budget_no_paid_action(self):
        api = Mock()
        api.get.return_value = pod()
        spawn = Mock(side_effect=lambda *args, **kwargs: FakeProcess(failed=True))
        clock = FakeClock(until=30)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(m.ClientError) as raised:
            m.tunnel_loop(self.config, api, clock, spawn, clock.clock)
        self.assertEqual(raised.exception.code, 75)
        self.assertEqual(spawn.call_count, 3)
        api.action.assert_not_called()

    def test_api_failure_closes_only_owned_child(self):
        api = Mock()
        api.get.side_effect = [pod()] + [m.ClientError("offline")] * 3
        proc = FakeProcess()
        clock = FakeClock(until=30)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(m.ClientError):
            m.tunnel_loop(self.config, api, clock, Mock(return_value=proc), clock.clock)
        self.assertTrue(proc.terminated)
        api.action.assert_not_called()

    def test_stop_event_reaps_child(self):
        api = Mock()
        api.get.return_value = pod()
        proc = FakeProcess()
        clock = FakeClock(until=0.5)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(m.tunnel_loop(self.config, api, clock, Mock(return_value=proc), clock.clock), 0)
        self.assertTrue(proc.terminated)

    def test_port_lock_exclusive(self):
        lock = m.TunnelLock(self.config)
        try:
            with self.assertRaises(BlockingIOError):
                m.TunnelLock(self.config)
        finally:
            lock.close()
        retry = m.TunnelLock(self.config)
        retry.close()


class WrapperTests(Fixture):
    def test_all_entrypoints_find_sibling_module(self):
        for name in ("runpod-bonsai", "runpod-tunnel", "runpod-serve", "runpod-ssh", "runpod-inspect"):
            with self.subTest(name=name):
                result = subprocess.run([str(ROOT / "bin" / name), "--help"],
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)

    def test_unknown_flags_rejected_before_api_access(self):
        for arguments in (("status", "--yes"), ("start", "--force"), ("not-a-command",)):
            result = subprocess.run([str(ROOT / "bin/runpod-bonsai"), *arguments],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 2)

    def test_cli_gpu_unavailable_exit_three(self):
        # Exercise real argument parser, config and HTTP client with mocked transport.
        path = self.config_file()
        def request(req, timeout):
            if req.method == "GET":
                return Response(pod(status="EXITED"))
            raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(
                b'{"detail":"There are not enough free GPUs on the host machine to start this pod."}'))
        fake_api = m.RunPodClient(self.config, request)
        with patch.object(m, "RunPodClient", return_value=fake_api), \
                patch.dict(os.environ, {"RUNPOD_BONSAI_CONFIG": str(path), "HOME": str(self.home)}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(m.main(["start", "--yes"]), 3)
        self.assertIn("GPU_UNAVAILABLE", err.getvalue())


if __name__ == "__main__":
    unittest.main()
