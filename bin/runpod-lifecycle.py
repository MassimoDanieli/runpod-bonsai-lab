#!/usr/bin/env python3
"""Bounded existing-Pod orchestration. No provisioning, deletion or auto-rollback.

Uses the shared REST/SSH client and a dedicated transient systemd user unit.
Paid starts require approval. Unknown action outcomes are reconciled, not retried.
The remote supervisor remains dry-run: this is not autonomous billing protection.
"""
import argparse
import contextlib
import copy
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("bonsai_shared_client", HERE / "runpod-client.py")
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)
Error = client.ClientError
ACTIVE_PHASES = {"starting", "watchdog_initializing", "loading", "ready", "unhealthy", "watchdog_restarting"}
FAILED_PHASES = {"server_failed", "failed", "dry_run_complete", "orphaned"}
RECORD_PHASES = {"start_requested", "running", "ready", "capacity_denied", "stopped", "adopted"}


class Deadline:
    def __init__(self, seconds, clock=time.monotonic, sleep=time.sleep):
        if not math.isfinite(seconds) or seconds <= 0:
            raise Error("Timeout must be finite and positive", 2)
        self.clock, self.sleep = clock, sleep
        self.ends = clock() + seconds

    def remaining(self):
        left = self.ends - self.clock()
        if left <= 0:
            raise Error("Lifecycle deadline expired; readiness/stopped state is NOT confirmed", 124)
        return left

    def pause(self, interval):
        self.sleep(min(interval, self.remaining()))


def retry_read(check, deadline, interval=2):
    """Retry a read-only not-ready result. Exceptions fail closed unless handled."""
    while True:
        deadline.remaining()
        result = check()
        deadline.remaining()
        if result:
            return result
        deadline.pause(interval)


@contextlib.contextmanager
def lifecycle_lock(config):
    client.private_directory(config.state_dir)
    fd = os.open(config.state_dir / f"lifecycle-{config.pod_id}.lock",
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise Error("Lifecycle lock must be a private owned regular file", 2)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Error("Another lifecycle operation owns this Pod; cancel it before retrying", 75) from None
        yield
    finally:
        os.close(fd)  # Preserve lock inode for other callers.


class Journal:
    """Only non-secret intent/session metadata; unresolved starts survive retries."""
    def __init__(self, config):
        self.config = config
        self.path = config.state_dir / f"lifecycle-{config.pod_id}.json"
        self.data = None
        if self.path.exists() or self.path.is_symlink():
            try:
                self.data = json.loads(client.private_file(self.path))
            except (ValueError, UnicodeError):
                raise Error("Invalid lifecycle record; do not silently reset an uncertain paid start", 2) from None
            data = self.data
            if (not isinstance(data, dict) or data.get("format") != 1
                    or data.get("pod_id") != config.pod_id or data.get("phase") not in RECORD_PHASES
                    or not isinstance(data.get("session_id"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", data["session_id"])
                    or type(data.get("started_at")) not in (int, float)
                    or not math.isfinite(data["started_at"]) or data["started_at"] < 0):
                raise Error("Invalid lifecycle record; operator review required", 2)

    def save(self):
        client.private_directory(self.config.state_dir)
        if self.path.is_symlink():
            raise Error("Refusing symlink lifecycle record", 2)
        fd, temporary = tempfile.mkstemp(prefix=".lifecycle-", dir=self.config.state_dir)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(self.data, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def new_intent(self, now=None):
        self.data = {"format": 1, "pod_id": self.config.pod_id,
                     "session_id": str(uuid.uuid4()), "started_at": time.time() if now is None else now,
                     "phase": "start_requested"}
        self.save()  # Write before the POST, including when a response may be lost.

    def phase(self, value):
        if value not in RECORD_PHASES:
            raise Error("Invalid lifecycle phase", 2)
        if self.data is not None:
            self.data["phase"] = value
            self.save()


def local_port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def ssh_owned_listener(main_pid, port, proc=Path("/proc")):
    """Read-only Linux check: a direct SSH child owns the loopback listener inode."""
    if type(main_pid) is not int or main_pid <= 1:
        return False
    try:
        inodes = set()
        for line in (proc / "net/tcp").read_text().splitlines()[1:]:
            row = line.split()
            if row[1] == f"0100007F:{port:04X}" and row[3] == "0A":
                inodes.add(row[9])
        # /proc/<pid>/task/<pid>/children is optional on some kernels.
        children = []
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
                if int(fields[1]) == main_pid:
                    children.append(entry.name)
            except (OSError, ValueError, IndexError):
                continue
        for pid in children:
            if not pid.isdigit():
                continue
            for entry in (proc / pid / "fd").iterdir():
                try:
                    link = os.readlink(entry)
                except OSError:
                    continue
                if link.startswith("socket:[") and link[8:-1] in inodes:
                    return True
    except (OSError, ValueError, IndexError):
        return False
    return False


def run_command(argv, timeout, runner=subprocess.run):
    try:
        result = runner(argv, stdin=subprocess.DEVNULL, capture_output=True,
                        env=client.child_environment(), timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise Error("Command deadline expired; remote operation may still be running", 124) from None
    except OSError:
        raise Error("Local command unavailable; check installation/user service manager", 2) from None
    if len(result.stdout) > client.BODY_LIMIT or len(result.stderr) > client.BODY_LIMIT:
        raise Error("Command output exceeds limit; content not displayed")
    return result


class UserTunnel:
    """Dedicated transient unit; no arbitrary PID kill and no unsafe nohup fallback."""
    def __init__(self, config, config_path, run=run_command):
        self.config, self.config_path, self.run = config, Path(config_path).absolute(), run
        token = hashlib.sha256(config.pod_id.encode()).hexdigest()[:24]
        self.unit = f"runpod-bonsai-{token}-{config.local_port}.service"
        public = {k: str(v) for k, v in vars(config).items() if k != "api_key"}
        public["config_path"] = str(self.config_path)
        public["client"] = str(HERE / "runpod-client.py")
        self.signature = hashlib.sha256(json.dumps(public, sort_keys=True).encode()).hexdigest()
        self.description = "Bonsai lifecycle " + self.signature

    def properties(self, deadline):
        result = self.run(["systemctl", "--user", "show", self.unit, "--no-pager",
                           "--property=LoadState,ActiveState,SubState,Description,Transient,MainPID"],
                          min(10, deadline.remaining()))
        try:
            values = dict(line.split("=", 1) for line in result.stdout.decode().splitlines() if "=" in line)
        except (UnicodeError, ValueError):
            raise Error("Invalid service-manager response") from None
        if values.get("LoadState") == "not-found":
            return None
        if result.returncode or values.get("LoadState") != "loaded":
            raise Error("User systemd manager unavailable; no detached fallback is attempted", 2)
        if values.get("Description") != self.description or values.get("Transient") != "yes":
            raise Error("Tunnel unit belongs to another configuration; refusing to adopt/stop it", 2)
        return values

    def preflight(self, deadline):
        for name in ("ssh", "systemctl", "systemd-run"):
            if shutil.which(name) is None:
                raise Error(f"Required local tool missing: {name}; no paid start attempted", 2)
        for path in (self.config_path, self.config.home, Path(sys.executable), HERE):
            if any(c in str(path) for c in ("$", "%", "\n", "\r")):
                raise Error("Lifecycle service paths must not contain systemd expansion characters", 2)
        properties = self.properties(deadline)
        if properties is not None and properties["ActiveState"] not in ("active", "activating"):
            raise Error("Previous lifecycle unit is not active; run down and review its journal", 2)
        if properties is None:
            if not local_port_free(self.config.local_port):
                raise Error("Local port is occupied by an unmanaged listener; not adopted or stopped", 2)
            try:
                lock = client.TunnelLock(self.config)
                lock.close()
            except BlockingIOError:
                raise Error("Another tunnel owns the local port; stop it explicitly first", 2) from None

    def start(self, deadline):
        current = self.properties(deadline)
        if current is not None:
            if current["ActiveState"] in ("active", "activating"):
                return False
            raise Error("Existing lifecycle tunnel failed; inspect it before restarting")
        if not local_port_free(self.config.local_port):
            raise Error("Local port became occupied; no listener was replaced")
        argv = ["systemd-run", "--user", "--quiet", "--collect", "--service-type=exec",
                "--unit=" + self.unit, "--description=" + self.description,
                "--property=Restart=no", "--property=KillMode=control-group",
                "--property=TimeoutStopSec=15", "--property=UMask=0077",
                "--property=NoNewPrivileges=yes", "--property=StandardInput=null",
                "--property=UnsetEnvironment=RUNPOD_API_KEY BONSAI_API_KEY",
                "--setenv=HOME=" + str(self.config.home),
                "--setenv=RUNPOD_BONSAI_CONFIG=" + str(self.config_path),
                "--", sys.executable, str(HERE / "runpod-client.py"), "tunnel"]
        result = self.run(argv, min(20, deadline.remaining()))
        if result.returncode:
            raise Error("Unable to start lifecycle tunnel; inspect user journal. Pod may be billable")
        return True

    def ready(self, deadline):
        properties = self.properties(deadline)
        if properties is None or properties["ActiveState"] not in ("active", "activating"):
            raise Error("Managed tunnel is not active; Pod may still be billable")
        try:
            pid = int(properties["MainPID"])
        except (KeyError, ValueError):
            raise Error("Invalid tunnel MainPID") from None
        return ssh_owned_listener(pid, self.config.local_port)

    def stop(self, deadline):
        current = self.properties(deadline)
        if current is None:
            return
        result = self.run(["systemctl", "--user", "stop", "--no-block", self.unit],
                          min(5, deadline.remaining()))
        if result.returncode:
            raise Error("Local tunnel stop was not accepted")
        def stopped():
            props = self.properties(deadline)
            return props is None or props["ActiveState"] in ("inactive", "failed")
        retry_read(stopped, deadline, 0.2)


class Operations:
    def __init__(self, config, config_path):
        self.config = config
        self.api = client.RunPodClient(config)
        self.tunnel = UserTunnel(config, config_path)

    def get(self, deadline):
        old = self.config.api_timeout
        self.config.api_timeout = min(old, deadline.remaining())
        try:
            return self.api.get()
        finally:
            self.config.api_timeout = old

    def action(self, action, deadline):
        old = self.config.api_timeout
        self.config.api_timeout = min(old, deadline.remaining())
        try:
            return self.api.action(action)
        finally:
            self.config.api_timeout = old

    def remote(self, command, deadline):
        endpoint = client.ssh_endpoint(self.get(deadline))
        cfg = copy.copy(self.config)
        cfg.ssh_timeout = max(1, min(cfg.ssh_timeout, int(deadline.remaining())))
        return run_command(client.ssh_arguments(cfg, endpoint, command), deadline.remaining())

    def ssh_probe(self, deadline):
        pod = self.get(deadline)
        if pod["status"] != "RUNNING":
            return False
        runtime = pod.get("runtime")
        if not isinstance(runtime, dict) or not runtime.get("ports"):
            return False
        # A malformed/ambiguous mapping is an error, not an unlimited retry.
        endpoint = client.ssh_endpoint(pod)
        result = run_command(client.ssh_arguments(self.config, endpoint, ["true"]),
                             min(self.config.ssh_timeout + 2, deadline.remaining()))
        return result.returncode == 0

    def pod_command(self, action, extra=()):
        return ["env", f"BONSAI_ROOT={self.config.remote_root}",
                f"BONSAI_PORT={self.config.remote_port}", f"RUNPOD_POD_ID={self.config.pod_id}",
                "BONSAI_WATCHDOG_DRY_RUN=1",
                "python3", self.config.remote_root + "/bin/bonsai-pod.py", action, *extra]

    def remote_status(self, deadline):
        result = self.remote(self.pod_command("status"), deadline)
        if result.returncode:
            raise Error("Remote supervisor status failed; install matching Pod scripts and inspect logs")
        try:
            data = json.loads(result.stdout)
        except (ValueError, UnicodeError):
            raise Error("Remote supervisor returned invalid JSON; output not displayed") from None
        if (not isinstance(data, dict) or data.get("phase") not in
                ACTIVE_PHASES | FAILED_PHASES | {"absent", "stopped", "stale", "stopping"}):
            raise Error("Unexpected remote supervisor status")
        if data["phase"] != "absent":
            if (type(data.get("supervisor_alive")) is not bool
                    or not isinstance(data.get("children_alive"), dict)
                    or any(type(v) is not bool for v in data["children_alive"].values())
                    or not isinstance(data.get("session_id"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", data["session_id"])):
                raise Error("Remote supervisor identity/liveness fields are invalid")
        return data

    def remote_start(self, deadline, record, reset):
        extra = []
        if record is not None:
            extra = ["--session-id", record["session_id"], "--session-start-epoch", str(record["started_at"])]
            if reset:
                extra += ["--new-session"]
        result = self.remote(self.pod_command("start", extra), deadline)
        if result.returncode:
            raise Error("Remote start did not confirm readiness; not retried. Inspect supervisor status/logs")

    def preflight(self, deadline):
        client.prepare_ssh(self.config)
        self.tunnel.preflight(deadline)

    def health(self, deadline):
        def opener(req, timeout):
            return client.open_request(req, min(timeout, deadline.remaining()))
        return client.health_ready(self.config.local_port, opener)


def wait_state(ops, desired, deadline, interval=2):
    def check():
        pod = ops.get(deadline)
        return pod if pod["status"] == desired else None
    return retry_read(check, deadline, interval)


def remote_ready(data):
    children = data.get("children_alive")
    return (data.get("phase") == "ready" and data.get("supervisor_alive") is True
            and isinstance(children, dict) and children.get("server") is True
            and children.get("watchdog") is True)


def up(ops, journal, deadline, yes=False, confirm=input):
    """No automatic cloud rollback: partial failures leave explicit recovery steps."""
    ops.preflight(deadline)  # Local requirements checked before any paid action.
    pod = ops.get(deadline)
    if pod["status"] == "EXITED":
        if journal.data and journal.data["phase"] == "start_requested":
            raise Error("Previous start is unresolved. Check the provider, then use down to acknowledge EXITED before another start")
        if not yes:
            try:
                approved = confirm("Start paid RunPod compute and prepare CLOUD? [yes/NO] ") == "yes"
            except EOFError:
                approved = False
            if not approved:
                print("Cancelled. No paid start requested.")
                return 0
        deadline.remaining()
        journal.new_intent()
        print("START_REQUESTED: existing Pod only; no provisioning or replacement")
        try:
            ops.action("start", deadline)
        except Error as exc:
            if exc.code == 3:
                journal.phase("capacity_denied")
                raise
            print("Start not confirmed; reconciling through GET only (no second POST)", file=sys.stderr)
        pod = wait_state(ops, "RUNNING", deadline)
        journal.phase("running")
    elif pod["status"] != "RUNNING":
        if not journal.data or journal.data["phase"] not in ("start_requested", "running"):
            raise Error("Pod is transitional without a local start intent; use wait/status first")
        pod = wait_state(ops, "RUNNING", deadline)
        journal.phase("running")
    elif journal.data and journal.data["phase"] == "start_requested":
        journal.phase("running")
    print("RUNNING: waiting for authenticated SSH; model readiness is separate")
    retry_read(lambda: ops.ssh_probe(deadline), deadline)
    status = ops.remote_status(deadline)
    active = status.get("supervisor_alive") is True
    record = journal.data if journal.data and journal.data["phase"] in ("running", "ready") else None
    fresh_run = bool(record and record["phase"] == "running"
                     and status.get("session_id") != record["session_id"] and not active)
    if (status["phase"] == "orphaned" or
            (status["phase"] in FAILED_PHASES and not
             (fresh_run and status["phase"] in ("failed", "dry_run_complete")))):
        raise Error("Remote supervisor requires review (failed/orphaned/expired); no session reset or process kill attempted")
    if active:
        if record and status.get("session_id") != record["session_id"]:
            raise Error("Running supervisor has a different session; not replaced or reset")
    else:
        if status["phase"] not in ("absent", "stopped", "stale", "failed", "dry_run_complete"):
            raise Error("Unexpected inactive supervisor phase; inspect before retrying")
        reset = bool(record and status["phase"] != "absent"
                     and status.get("session_id") != record["session_id"])
        ops.remote_start(deadline, record, reset)
    def ready():
        state = ops.remote_status(deadline)
        if state["phase"] in FAILED_PHASES or state["phase"] in ("stopped", "stale", "absent"):
            raise Error("Model startup failed; Pod remains potentially billable")
        if record and state.get("session_id") != record["session_id"]:
            raise Error("Remote session changed during readiness wait")
        return state if remote_ready(state) else None
    retry_read(ready, deadline)
    ops.tunnel.start(deadline)
    retry_read(lambda: ops.tunnel.ready(deadline) and ops.health(deadline), deadline, 0.5)
    # Final cross-check; localhost health alone cannot identify a remote Pod.
    pod = ops.get(deadline)
    if pod["status"] != "RUNNING" or not remote_ready(ops.remote_status(deadline)):
        raise Error("Readiness changed during final verification; CLOUD is not confirmed ready")
    if record:
        journal.phase("ready")
    print("READY: owned SSH tunnel + remote model/watchdog + /health verified. Watchdog is DRY-RUN; use down to stop billing.")
    return 0


def down(ops, journal, deadline, cleanup_seconds=10):
    cleanup_error = None
    try:
        local_budget = Deadline(min(cleanup_seconds, deadline.remaining() / 4))
        ops.tunnel.stop(local_budget)
    except (Error, OSError) as exc:
        cleanup_error = type(exc).__name__
        print("Local tunnel cleanup failed; continuing with the requested Pod stop", file=sys.stderr)
    pod = ops.get(deadline)
    if pod["status"] != "EXITED":
        try:
            ops.action("stop", deadline)
        except Error:
            print("Stop request outcome unknown; reconciling through GET only", file=sys.stderr)
        wait_state(ops, "EXITED", deadline)
    if journal is not None:
        journal.phase("stopped")
    print("EXITED confirmed by RunPod. Persistent storage can remain billable.")
    if cleanup_error:
        raise Error("Pod is EXITED but local tunnel cleanup needs attention; no unrelated unit/process was killed")
    return 0


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    for name in ("up", "down", "wait"):
        command = sub.add_parser(name)
        command.add_argument("--timeout", type=float, default=600, help="Total wait budget in seconds (default: 600)")
        if name == "up":
            command.add_argument("--yes", action="store_true", help="Explicit paid-start approval")
        if name == "wait":
            command.add_argument("--state", choices=("RUNNING", "EXITED"), default="RUNNING")
    return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    if not math.isfinite(arguments.timeout) or not 1 <= arguments.timeout <= 3600:
        parser().error("--timeout must be finite and between 1 and 3600 seconds")
    try:
        config = client.Config.load()
        config_path = Path(os.environ.get("RUNPOD_BONSAI_CONFIG", config.home / ".config/runpod.env"))
        operations = Operations(config, config_path)
        deadline = Deadline(arguments.timeout)
        if arguments.command == "wait":
            wait_state(operations, arguments.state, deadline)
            print(f"{arguments.state} confirmed. This is Pod state, not model readiness.")
            return 0
        with lifecycle_lock(config):
            if arguments.command == "up":
                return up(operations, Journal(config), deadline, arguments.yes)
            try:
                journal = Journal(config)
            except Error:
                print("Local session record invalid; continuing requested Pod stop without overwriting it", file=sys.stderr)
                journal = None
            return down(operations, journal, deadline)
    except Error as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print("No automatic cloud rollback. Check status; use runpod-bonsai down for an intentional Pod stop.", file=sys.stderr)
        return exc.code
    except OSError:
        print("ERROR: local operation failed. Check private files/tools; Pod state/billing may be unchanged.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. No automatic Pod stop; check status and use down if required.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    def terminate(_signal, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    raise SystemExit(main())
