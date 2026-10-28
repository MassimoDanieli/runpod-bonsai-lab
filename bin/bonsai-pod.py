#!/usr/bin/env python3
"""Linux Pod-local supervision. Dry-run watchdog only; never calls RunPod APIs.

Not a Pod boot hook or a billing stop. Run under an external supervisor for
recovery from SIGKILL, OOM, host/container failure. State storage must be private.
"""
import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import select
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

HERE = Path(__file__).resolve().parent
BODY_LIMIT = 1024 * 1024
IDENTITY_KEYS = {"pid", "start_ticks", "boot_id", "pid_namespace", "exe", "argv_hash", "uid"}


class SafetyError(Exception):
    pass


def bounded(name, default, low, high, integer=False):
    raw = os.getenv(name, str(default))
    try:
        number = int(raw) if integer else float(raw)
    except ValueError:
        raise SafetyError(f"Invalid {name}") from None
    if not math.isfinite(number) or not low <= number <= high:
        raise SafetyError(f"{name} must be in [{low}, {high}]")
    return number


def private_path(path, directory=False):
    path = Path(os.path.abspath(Path(path).expanduser()))
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise SafetyError("State/log path must not contain symlinks")
    if directory:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise SafetyError(f"Runtime directory must be owned and mode 700: {path}")
    return path


def private_fd(path, flags):
    path = private_path(path)
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        os.close(fd)
        raise SafetyError("Runtime file must be an owned private regular file")
    return fd


@contextlib.contextmanager
def lock_file(path, timeout=0):
    fd = private_fd(path, os.O_RDWR | os.O_CREAT)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SafetyError("Another launcher/supervisor holds the runtime lock") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)  # Never unlink a lockfile: its inode coordinates other callers.


def load_json(path):
    try:
        fd = private_fd(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as handle:
        raw = handle.read(16385)
    if len(raw) > 16384:
        raise SafetyError("Runtime record exceeds size limit")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise SafetyError("Corrupt runtime record; refusing to guess process ownership") from None
    if not isinstance(data, dict):
        raise SafetyError("Invalid runtime record")
    return data


def save_json(path, data):
    private_path(path)
    fd, name = tempfile.mkstemp(prefix=".runtime-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def identity(pid):
    """Fingerprint a living Linux process; numeric PID alone is never trusted."""
    if type(pid) is not int or pid <= 1:
        return None
    try:
        base = Path(f"/proc/{pid}")
        fields = (base / "stat").read_text().rsplit(") ", 1)[1].split()
        if fields[0] in ("Z", "X"):
            return None
        exe = (base / "exe").stat()
        return {"pid": pid, "start_ticks": int(fields[19]),
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "pid_namespace": os.readlink(base / "ns/pid"),
                "exe": [exe.st_dev, exe.st_ino], "uid": base.stat().st_uid,
                "argv_hash": hashlib.sha256((base / "cmdline").read_bytes()).hexdigest()}
    except (OSError, ValueError, IndexError):
        return None


def matches(record):
    return (isinstance(record, dict) and set(record) == IDENTITY_KEYS
            and identity(record.get("pid")) == record)


def signal_owned(record, sig):
    """Bind to the process with pidfd, then recheck identity before signalling."""
    if not matches(record):
        return False
    try:
        fd = os.pidfd_open(record["pid"])
    except ProcessLookupError:
        return False
    try:
        if not matches(record):
            return False
        signal.pidfd_send_signal(fd, sig)
        return True
    except ProcessLookupError:
        return False
    finally:
        os.close(fd)


def runtime_record(cfg):
    data = load_json(cfg.state / "processes.json")
    if data is not None and (data.get("format") != 1 or not isinstance(data.get("children"), dict)):
        raise SafetyError("Invalid process record")
    return data


def live_children(data):
    return [record for record in (data or {}).get("children", {}).values() if matches(record)]


class RotatingLog:
    """Bound both live log growth and retained files; each instance has one writer."""
    def __init__(self, path, limit, backups=2):
        self.path, self.limit, self.backups = path, limit, backups
        self.handle = None
        self._open()

    def _open(self):
        fd = private_fd(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        self.size = os.fstat(fd).st_size
        self.handle = os.fdopen(fd, "ab", buffering=0)

    def rotate(self):
        self.handle.close()
        self.handle = None
        for index in range(self.backups, 0, -1):
            old = self.path if index == 1 else Path(f"{self.path}.{index - 1}")
            new = Path(f"{self.path}.{index}")
            private_path(old)
            private_path(new)
            if old.exists():
                os.replace(old, new)
        self._open()

    def write(self, data):
        while data:
            if self.size >= self.limit:
                self.rotate()
            block, data = data[:self.limit - self.size], data[self.limit - self.size:]
            self.handle.write(block)
            self.size += len(block)

    def close(self):
        if self.handle:
            self.handle.close()
            self.handle = None


class Child:
    def __init__(self, command, env, cwd, log, ready=False):
        self.log, self.error, self.thread = log, None, None
        read_fd, write_fd = os.pipe() if ready else (None, None)
        if ready:
            command = command + ["--ready-fd", str(write_fd)]
        # The watchdog writes directly to a file, not a supervisor-owned pipe.
        # It must survive an abrupt supervisor death without BrokenPipeError.
        if ready and os.fstat(log.handle.fileno()).st_size >= log.limit:
            log.rotate()
        try:
            self.process = subprocess.Popen(command, cwd=cwd, env=env,
                                            stdin=subprocess.DEVNULL, stdout=log.handle if ready else subprocess.PIPE,
                                            stderr=subprocess.STDOUT, start_new_session=True,
                                            pass_fds=(write_fd,) if ready else ())
        except BaseException:
            if read_fd is not None:
                os.close(read_fd)
            raise
        finally:
            if write_fd is not None:
                os.close(write_fd)
        self.fingerprint = identity(self.process.pid)
        if not ready:
            self.thread = threading.Thread(target=self._drain, daemon=True)
            self.thread.start()
        self.ready_fd = read_fd

    def _drain(self):
        try:
            while True:
                chunk = os.read(self.process.stdout.fileno(), 8192)
                if not chunk:
                    break
                self.log.write(chunk)
        except OSError as exc:
            self.error = type(exc).__name__
        finally:
            self.process.stdout.close()

    def initialized(self, timeout):
        """A watchdog handshake means config and persistent session locks passed."""
        try:
            if not select.select([self.ready_fd], [], [], timeout)[0]:
                return False
            return os.read(self.ready_fd, 32) == b"READY\n" and self.process.poll() is None
        finally:
            os.close(self.ready_fd)
            self.ready_fd = None

    def close(self):
        if self.ready_fd is not None:
            os.close(self.ready_fd)
            self.ready_fd = None
        # Popen owns this direct child; unlike a persisted PID, it is safe to reap.
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        if self.thread:
            self.thread.join(timeout=2)


class Config:
    def __init__(self):
        if sys.platform != "linux" or not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise SafetyError("Requires Linux with pidfd support and Python 3.11+")
        self.root = Path(os.getenv("BONSAI_ROOT", "/workspace/ai/Bonsai-demo")).expanduser().absolute()
        self.state = private_path(os.getenv("BONSAI_STATE_DIR", str(self.root / "run")), directory=True)
        self.binary = Path(os.getenv("BONSAI_SERVER_BIN", str(self.root / "bin/cuda/llama-server"))).absolute()
        self.model = Path(os.getenv("BONSAI_MODEL_PATH", str(self.root / "models/bonsai2-gguf/27B/Ternary-Bonsai-2-27B-PQ2_0.gguf"))).absolute()
        self.watchdog = Path(os.getenv("BONSAI_WATCHDOG", str(HERE / "bonsai-watchdog.py"))).absolute()
        self.port = bounded("BONSAI_PORT", 18080, 1, 65535, True)
        self.context = bounded("BONSAI_CTX", 65536, 512, 1048576, True)
        self.layers = bounded("BONSAI_NGL", 99, 0, 999, True)
        self.ready_timeout = bounded("BONSAI_READY_TIMEOUT", 180, 0.2, 3600)
        self.poll = bounded("BONSAI_POLL_SECONDS", 1, 0.02, 30)
        self.probe_timeout = bounded("BONSAI_PROBE_TIMEOUT", 2, 0.05, 10)
        self.idle = bounded("BONSAI_IDLE_MINUTES", 45, 0.001, 10080)
        self.unhealthy = bounded("BONSAI_UNHEALTHY_MINUTES", 90, 0.001, 10080)
        self.max_hours = bounded("BONSAI_MAX_HOURS", 8, 0.0001, 168)
        self.interval = bounded("BONSAI_WATCHDOG_INTERVAL", 30, 0.02, 300)
        self.restart_limit = bounded("BONSAI_WATCHDOG_RESTARTS", 3, 0, 10, True)
        self.restart_delay = bounded("BONSAI_RESTART_DELAY", 1, 0.05, 30)
        self.handshake_timeout = bounded("BONSAI_WATCHDOG_INIT_TIMEOUT", 10, 0.1, 120)
        self.log_limit = bounded("BONSAI_LOG_BYTES", 10485760, 1024, 1073741824, True)
        self.url = f"http://127.0.0.1:{self.port}"
        self.pod_id = os.getenv("RUNPOD_POD_ID", "")

    def validate_files(self):
        if os.getenv("BONSAI_WATCHDOG_DRY_RUN", "1") != "1":
            raise SafetyError("This supervisor supports dry-run watchdogs only; no process started")
        if not self.root.is_dir() or not os.access(self.binary, os.X_OK) or not self.model.is_file() or not self.watchdog.is_file():
            raise SafetyError("Missing root, executable model server, GGUF or watchdog script")

    def model_command(self):
        return [str(self.binary), "-m", str(self.model), "-ngl", str(self.layers), "-c", str(self.context),
                "--host", "127.0.0.1", "--port", str(self.port), "--slots"]

    def child_env(self):
        # Model files are local. No cloud/API tokens are needed by either child.
        allowed = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "LD_LIBRARY_PATH",
                   "CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER", "NVIDIA_VISIBLE_DEVICES",
                   "NVIDIA_DRIVER_CAPABILITIES", "OMP_NUM_THREADS"}
        return {**{k: v for k, v in os.environ.items() if k in allowed}, "PYTHONUNBUFFERED": "1"}


def watchdog_module():
    spec = importlib.util.spec_from_file_location("supervised_watchdog", HERE / "bonsai-watchdog.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare_session(cfg, args):
    path = cfg.state / "watchdog-session.json"
    previous = load_json(path)
    session_id = args.session_id or (previous or {}).get("session_id") or str(uuid.uuid4())
    if args.new_session and not args.session_id:
        raise SafetyError("--new-session requires an explicitly different --session-id")
    record = watchdog_module().SessionRecord(path, cfg.pod_id, session_id, time.time(),
                                            args.new_session, args.session_start_epoch)
    try:
        if record.elapsed >= cfg.max_hours * 3600:
            raise SafetyError("Session ceiling already reached; authorize a new Pod session explicitly")
        return session_id, record.data["started_at"]
    finally:
        record.close()


def port_available(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            raise SafetyError("Model port is already occupied; refusing to adopt an unmanaged server") from None


def probe_ready(cfg):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        results = []
        for endpoint in ("/health", "/slots"):
            with opener.open(cfg.url + endpoint, timeout=cfg.probe_timeout) as response:
                raw = response.read(BODY_LIMIT + 1)
            if len(raw) > BODY_LIMIT:
                return False
            results.append(json.loads(raw))
        health, slots = results
        return (isinstance(health, dict) and health.get("status") == "ok"
                and isinstance(slots, list) and bool(slots)
                and all(isinstance(s, dict) and type(s.get("is_processing")) is bool for s in slots))
    except (OSError, ValueError, urllib.error.URLError):
        return False


def supervised_run(cfg, args):
    cfg.validate_files()
    with lock_file(cfg.state / "supervisor.lock"):
        old = runtime_record(cfg)
        if live_children(old):
            raise SafetyError("Previous owned children still live; inspect and run stop before restarting")
        port_available(cfg.port)
        session_id, origin = prepare_session(cfg, args)
        logs = {name: RotatingLog(cfg.state / f"{name}.log", cfg.log_limit)
                for name in ("server", "watchdog", "supervisor")}
        children = {}
        stop_event = threading.Event()
        previous_handlers = {}
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[sig] = signal.signal(sig, lambda *_: stop_event.set())
        data = {"format": 1, "supervisor": identity(os.getpid()), "children": {},
                "session_id": session_id, "session_origin": origin, "phase": "starting",
                "watchdog_mode": "dry-run", "port": cfg.port}

        def publish(phase, detail=""):
            # Popen owns an unreaped direct child even during its initial exec.
            # Refresh the snapshot after initialization/readiness, not from a PID file.
            for child in children.values():
                if child.process.poll() is None:
                    current = identity(child.process.pid)
                    if current is not None:
                        child.fingerprint = current
            if (phase == data["phase"] and detail == data.get("detail", "")
                    and time.time() - data.get("observed_at", 0) < 5):
                return
            if phase != data["phase"] or detail:
                logs["supervisor"].write(f"{time.time():.3f} {phase} {detail}\n".encode())
            data.update(phase=phase, detail=detail, observed_at=time.time(),
                        children={key: child.fingerprint for key, child in children.items()})
            save_json(cfg.state / "processes.json", data)

        def start_watchdog():
            command = [sys.executable, str(cfg.watchdog), "--url", cfg.url + "/slots",
                       "--idle-minutes", str(cfg.idle), "--unhealthy-minutes", str(cfg.unhealthy),
                       "--max-hours", str(cfg.max_hours), "--interval-seconds", str(cfg.interval),
                       "--session-id", session_id, "--pod-id", cfg.pod_id,
                       "--state-file", str(cfg.state / "watchdog-session.json")]
            child = Child(command, cfg.child_env(), cfg.root, logs["watchdog"], ready=True)
            children["watchdog"] = child
            publish("watchdog_initializing")
            if not child.initialized(cfg.handshake_timeout):
                child.close()
                raise SafetyError("Watchdog did not confirm initialization; model will not start")

        outcome = 1
        try:
            publish("starting")
            start_watchdog()
            if stop_event.is_set():
                return 0
            children["server"] = Child(cfg.model_command(), cfg.child_env(), cfg.root, logs["server"])
            publish("loading")
            deadline = time.monotonic() + cfg.ready_timeout
            restarts = 0
            server_failed = False
            while not stop_event.is_set():
                watchdog = children["watchdog"]
                if any(child.error for child in children.values()):
                    raise SafetyError("Child log writer failed; stopping managed processes")
                wd_code = watchdog.process.poll()
                if wd_code is not None:
                    children["server"].close()
                    server_failed = True
                    if wd_code == 0:
                        publish("dry_run_complete", "Model stopped locally; the GPU Pod is NOT stopped")
                        outcome = 0
                        break
                    if restarts >= cfg.restart_limit:
                        raise SafetyError("Watchdog restart budget exhausted; GPU Pod is NOT stopped")
                    restarts += 1
                    watchdog.close()
                    publish("watchdog_restarting", f"attempt={restarts}; model held stopped")
                    if stop_event.wait(min(cfg.restart_delay * (2 ** (restarts - 1)), 30)):
                        break
                    start_watchdog()  # Same session file/ID; never reset the cost ceiling.
                    publish("server_failed", "Watchdog recovered; model remains stopped")
                server = children["server"]
                if server.process.poll() is not None and not server_failed:
                    server_failed = True
                    publish("server_failed", "Model exited; independent watchdog remains running")
                if not server_failed:
                    ready = probe_ready(cfg)
                    if ready:
                        publish("ready")
                    elif data["phase"] == "ready":
                        publish("unhealthy", "Model endpoint unavailable; watchdog remains running")
                    if data["phase"] == "loading" and time.monotonic() >= deadline:
                        server.close()
                        server_failed = True
                        publish("server_failed", "Readiness timeout; model stopped, watchdog remains running")
                stop_event.wait(cfg.poll)
            if stop_event.is_set():
                outcome = 0
                publish("stopping", "Operator requested local process stop; GPU Pod is NOT stopped")
        except (OSError, SafetyError, ValueError) as exc:
            publish("failed", str(exc))
            outcome = 1
        finally:
            for child in children.values():
                child.close()
            if data["phase"] not in ("failed", "dry_run_complete"):
                publish("stopped", "Local processes stopped; GPU billing is unaffected")
            else:
                publish(data["phase"])
            for log in logs.values():
                log.close()
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
        return outcome


def start(cfg, args):
    cfg.validate_files()
    with lock_file(cfg.state / "launch.lock", cfg.ready_timeout + 5):
        old = runtime_record(cfg)
        if matches((old or {}).get("supervisor")):
            if args.new_session or (args.session_id and args.session_id != old["session_id"]):
                raise SafetyError("Cannot reset/change an active session")
            if old.get("phase") == "ready" and len(live_children(old)) == 2 and probe_ready(cfg):
                print("Already ready (managed server and dry-run watchdog).")
                return 0
            raise SafetyError(f"Supervisor is active ({old.get('phase')}); inspect status/logs, do not duplicate")
        if live_children(old):
            raise SafetyError("Owned orphan processes remain; inspect status and stop before starting")
        command = [sys.executable, str(Path(__file__).resolve()), "run"]
        if args.session_id:
            command.extend(["--session-id", args.session_id])
        if args.new_session:
            command.append("--new-session")
        if args.session_start_epoch is not None:
            command.extend(["--session-start-epoch", str(args.session_start_epoch)])
        # All ongoing logs are bounded by the supervisor. Startup diagnostics are
        # kept in a fresh private file, never inherited from the SSH terminal.
        fd = private_fd(cfg.state / "launch.log", os.O_WRONLY | os.O_CREAT)
        with os.fdopen(fd, "wb") as output:
            output.truncate(0)
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + cfg.ready_timeout + cfg.handshake_timeout + 5
        while time.monotonic() < deadline:
            current = runtime_record(cfg)
            if ((current or {}).get("supervisor") or {}).get("pid") == process.pid:
                phase = current["phase"]
                if phase == "ready" and matches(current["supervisor"]) and len(live_children(current)) == 2:
                    print("READY: model /health and /slots validated; watchdog is DRY-RUN.")
                    print("GPU Pod billing remains active until explicitly stopped.")
                    return 0
                if phase in ("server_failed", "failed", "dry_run_complete", "stopped"):
                    raise SafetyError(f"Startup is {phase}. Inspect status and private logs; GPU Pod was NOT stopped")
            if process.poll() is not None:
                raise SafetyError("Supervisor exited before readiness; inspect launch.log; Pod was NOT stopped")
            time.sleep(min(cfg.poll, 0.1))
        raise SafetyError("Readiness wait expired; inspect supervisor/watchdog status; Pod was NOT stopped")


def status(cfg):
    data = runtime_record(cfg)
    if data is None:
        print(json.dumps({"phase": "absent", "pod_billing": "not controlled"}))
        return 0
    data["supervisor_alive"] = matches(data.get("supervisor"))
    data["children_alive"] = {name: matches(record) for name, record in data["children"].items()}
    if not data["supervisor_alive"] and any(data["children_alive"].values()):
        data["phase"] = "orphaned"
    elif not data["supervisor_alive"] and data["phase"] not in ("stopped", "failed", "dry_run_complete"):
        data["phase"] = "stale"
    print(json.dumps(data, indent=2))
    return 0


def stop(cfg):
    with lock_file(cfg.state / "launch.lock", 5):
        data = runtime_record(cfg)
        if not data:
            print("No managed processes recorded; no signals sent.")
            return 0
        manager = data.get("supervisor")
        if matches(manager):
            signal_owned(manager, signal.SIGTERM)
            deadline = time.monotonic() + 12
            while matches(manager) and time.monotonic() < deadline:
                time.sleep(0.05)
            if matches(manager):
                raise SafetyError("Supervisor did not stop; no unsafe PID fallback used")
        # Handle only verified owned orphans if the manager was killed or crashed.
        for child in live_children(data):
            signal_owned(child, signal.SIGTERM)
            deadline = time.monotonic() + 3
            while matches(child) and time.monotonic() < deadline:
                time.sleep(0.05)
            if matches(child):
                signal_owned(child, signal.SIGKILL)
        print("Local managed processes stopped (or no longer match); GPU Pod is NOT stopped.")
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "run", "status", "stop"))
    parser.add_argument("--session-id", default=os.getenv("BONSAI_SESSION_ID", ""))
    parser.add_argument("--new-session", action="store_true")
    parser.add_argument("--session-start-epoch", type=float)
    args = parser.parse_args()
    try:
        cfg = Config()
        if args.command == "start":
            return start(cfg, args)
        if args.command == "run":
            return supervised_run(cfg, args)
        return status(cfg) if args.command == "status" else stop(cfg)
    except (OSError, SafetyError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
