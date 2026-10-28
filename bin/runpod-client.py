#!/usr/bin/env python3
"""Shared RunPod controller/SSH client. No provisioning or lifecycle orchestration.

Configuration is data, not sourced shell code. SSH and tunnel children never
receive the API credential. The Pod-local watchdog has a separate credential
contract and is deliberately not changed here.
"""
import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

API_BASE = "https://api.runpod.io/v2/pods"
BODY_LIMIT = 1024 * 1024
CONFIG_LIMIT = 65536
POD_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
SETTINGS = {
    "RUNPOD_POD_ID", "RUNPOD_API_KEY", "RUNPOD_LOCAL_PORT", "RUNPOD_REMOTE_PORT",
    "RUNPOD_SSH_KEY", "RUNPOD_KNOWN_HOSTS", "RUNPOD_SSH_USER",
    "RUNPOD_SSH_HOST_KEY_POLICY", "RUNPOD_SSH_TIMEOUT", "RUNPOD_API_TIMEOUT",
    "BONSAI_REMOTE_ROOT", "RUNPOD_TUNNEL_REFRESH_SECONDS",
    "RUNPOD_TUNNEL_RETRY_SECONDS", "RUNPOD_TUNNEL_MAX_FAILURES",
    "RUNPOD_STATE_DIR",
}


class ClientError(Exception):
    """Messages must never contain credentials, arbitrary API bodies or env values."""
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_request(request, timeout):
    # No redirects carrying Authorization, no implicit environment HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    return opener.open(request, timeout=timeout)


def private_file(path, limit=CONFIG_LIMIT):
    """Open without following links and require private, current-user ownership."""
    path = Path(path)
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ClientError("Private file path must not traverse symlinks", 2)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077):
                raise ClientError("Private file must be owned by this user and mode 600 or stricter", 2)
            raw = handle.read(limit + 1)
    except OSError:
        raise ClientError("Cannot read private file; check path, ownership and permissions", 2) from None
    if len(raw) > limit:
        raise ClientError("Private file exceeds size limit", 2)
    return raw


def parse_config(text):
    result = {}
    for line_number, line in enumerate(text.splitlines(), 1):
        try:
            tokens = shlex.split(line, comments=True, posix=True)
        except ValueError:
            raise ClientError(f"Invalid quoting in configuration line {line_number}", 2) from None
        if not tokens:
            continue
        if tokens[0] == "export":
            tokens = tokens[1:]
        if len(tokens) != 1 or "=" not in tokens[0]:
            raise ClientError(f"Expected one NAME=value assignment on line {line_number}", 2)
        key, value = tokens[0].split("=", 1)
        if key not in SETTINGS:
            raise ClientError(f"Unsupported configuration key on line {line_number}; see CONFIGURATION.md", 2)
        if key in result:
            raise ClientError(f"Duplicate configuration key on line {line_number}", 2)
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ClientError(f"Control character in configuration line {line_number}", 2)
        result[key] = value
    return result


def integer(value, label, minimum=1, maximum=65535):
    text = str(value)
    if (isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,10}", text)
            or not minimum <= int(text) <= maximum):
        raise ClientError(f"Invalid {label}; expected integer {minimum}..{maximum}", 2)
    return int(text)


def absolute_path(value, label, home):
    if value in ("~", "$HOME", "${HOME}"):
        value = str(home)
    for prefix in ("~/", "$HOME/", "${HOME}/"):
        if value.startswith(prefix):
            value = str(home) + "/" + value[len(prefix):]
            break
    if (not value.startswith("/") or "\x00" in value or "$" in value or "%" in value
            or any(ord(c) < 32 for c in value)
            or any(p in (".", "..") for p in value.split("/"))):
        raise ClientError(f"Invalid {label}; use an absolute path or a HOME prefix", 2)
    return Path(value)


class Config:
    def __init__(self, values, home):
        self.home = Path(home)
        self.pod_id = values.get("RUNPOD_POD_ID", "")
        self.api_key = values.get("RUNPOD_API_KEY", "")
        if not POD_ID.fullmatch(self.pod_id):
            raise ClientError("Missing or invalid RUNPOD_POD_ID", 2)
        if (not self.api_key or len(self.api_key) > 4096
                or any(ord(c) < 33 or ord(c) > 126 for c in self.api_key)):
            raise ClientError("Missing or invalid RUNPOD_API_KEY", 2)
        self.local_port = integer(values.get("RUNPOD_LOCAL_PORT", "18082"), "local port")
        self.remote_port = integer(values.get("RUNPOD_REMOTE_PORT", "18080"), "remote port")
        self.api_timeout = integer(values.get("RUNPOD_API_TIMEOUT", "30"), "API timeout", 1, 120)
        self.ssh_timeout = integer(values.get("RUNPOD_SSH_TIMEOUT", "10"), "SSH timeout", 1, 120)
        self.refresh_seconds = integer(values.get("RUNPOD_TUNNEL_REFRESH_SECONDS", "30"), "refresh interval", 1, 300)
        self.retry_seconds = integer(values.get("RUNPOD_TUNNEL_RETRY_SECONDS", "5"), "retry interval", 1, 60)
        self.max_failures = integer(values.get("RUNPOD_TUNNEL_MAX_FAILURES", "6"), "failure budget", 1, 100)
        self.key = absolute_path(values.get("RUNPOD_SSH_KEY", "$HOME/.ssh/runpod_ed25519"), "SSH key path", self.home)
        self.known_hosts = absolute_path(values.get("RUNPOD_KNOWN_HOSTS", "$HOME/.ssh/known_hosts_runpod"), "known_hosts path", self.home)
        self.state_dir = absolute_path(values.get("RUNPOD_STATE_DIR", "$HOME/.local/state/runpod-bonsai"), "state directory", self.home)
        self.remote_root = values.get("BONSAI_REMOTE_ROOT", "/workspace/ai/Bonsai-demo").rstrip("/")
        if (not self.remote_root or not self.remote_root.startswith("/")
                or any(p in ("", ".", "..") for p in self.remote_root.split("/")[1:])
                or any(ord(c) < 32 or ord(c) == 127 for c in self.remote_root)):
            raise ClientError("BONSAI_REMOTE_ROOT must be a canonical absolute POSIX path", 2)
        self.ssh_user = values.get("RUNPOD_SSH_USER", "root")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,63}", self.ssh_user):
            raise ClientError("Invalid SSH user", 2)
        self.host_policy = values.get("RUNPOD_SSH_HOST_KEY_POLICY", "accept-new")
        if self.host_policy not in ("yes", "accept-new"):
            raise ClientError("SSH host-key policy must be yes or accept-new; disabling it is forbidden", 2)

    @classmethod
    def load(cls, environ=None):
        env = os.environ if environ is None else environ
        home = Path(env.get("HOME", str(Path.home())))
        path = Path(env.get("RUNPOD_BONSAI_CONFIG", str(home / ".config/runpod.env")))
        try:
            text = private_file(path).decode("utf-8")
        except UnicodeError:
            raise ClientError("Configuration must be UTF-8", 2) from None
        return cls(parse_config(text), home)


def validate_pod(data, expected_id):
    if (not isinstance(data, dict) or data.get("id") != expected_id
            or not isinstance(data.get("status"), str)
            or not re.fullmatch(r"[A-Z][A-Z_]{0,63}", data["status"])):
        raise ClientError("Unexpected Pod identity or status in RunPod response")
    return data


def ssh_endpoint(pod):
    if pod["status"] != "RUNNING":
        raise ClientError("Pod is not RUNNING; no SSH connection attempted", 4)
    runtime = pod.get("runtime")
    if not isinstance(runtime, dict) or not isinstance(runtime.get("ports"), list):
        raise ClientError("No active SSH port mapping in RunPod response")
    matches = [p for p in runtime["ports"] if isinstance(p, dict)
               and p.get("type") == "tcp" and type(p.get("private")) is int and p["private"] == 22]
    if len(matches) != 1:
        raise ClientError("Expected exactly one direct TCP SSH mapping")
    mapping = matches[0]
    host = mapping.get("ip")
    if not isinstance(host, str) or "%" in host:
        raise ClientError("Invalid SSH IP address in RunPod response")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise ClientError("Invalid SSH IP address in RunPod response") from None
    if ip.is_unspecified or ip.is_loopback or ip.is_multicast or ip.is_link_local:
        raise ClientError("Refusing a non-routable SSH destination")
    port = mapping.get("public")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ClientError("Invalid public SSH port in RunPod response")
    return str(ip), port


class RunPodClient:
    def __init__(self, config, opener=open_request):
        self.config = config
        self.opener = opener
        self.url = f"{API_BASE}/{config.pod_id}"

    def request(self, action=None):
        if action not in (None, "start", "stop"):
            raise ClientError("Unsupported Pod action", 2)
        request = urllib.request.Request(
            self.url + ("/action" if action else ""),
            data=json.dumps({"action": action}).encode() if action else None,
            method="POST" if action else "GET",
            headers={"Authorization": "Bearer " + self.config.api_key,
                     "Content-Type": "application/json"},
        )
        try:
            with self.opener(request, self.config.api_timeout) as response:
                if not 200 <= response.status < 300:
                    raise ClientError(f"RUNPOD_API_ERROR: HTTP {response.status}")
                raw = response.read(BODY_LIMIT + 1)
        except urllib.error.HTTPError as exc:
            # Inspect only a bounded capacity-error message; never echo body/env.
            detail = ""
            try:
                body = json.loads(exc.read(BODY_LIMIT))
                if isinstance(body, dict) and isinstance(body.get("detail"), str):
                    detail = body["detail"].lower()
            except (OSError, ValueError, UnicodeError):
                pass
            finally:
                exc.close()
            if action == "start" and exc.code == 400 and "not enough free gpus" in detail:
                raise ClientError("GPU_UNAVAILABLE: the original host has no free GPUs.\n"
                                  "The existing Pod was preserved. No additional Pod was created.", 3) from None
            raise ClientError(f"RUNPOD_API_ERROR: HTTP {exc.code}; no automatic action retry") from None
        except (OSError, urllib.error.URLError, TimeoutError):
            raise ClientError("RunPod transport failure. Action outcome may be unknown; check status before retrying.") from None
        if len(raw) > BODY_LIMIT:
            raise ClientError("RunPod response exceeds size limit; check status before retrying")
        if action:
            return None  # Acceptance is not readiness or confirmed stop.
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ClientError("RunPod returned invalid JSON") from None
        return validate_pod(data, self.config.pod_id)

    def get(self):
        return self.request()

    def action(self, action):
        return self.request(action)


def health_ready(port, opener=open_request):
    try:
        with opener(f"http://127.0.0.1:{port}/health", 3) as response:
            if response.status != 200:
                return False
            raw = response.read(4097)
        return len(raw) <= 4096 and json.loads(raw) == {"status": "ok"}
    except (OSError, urllib.error.URLError, ValueError, UnicodeError):
        return False


def safe_name(value):
    if not isinstance(value, str):
        return "(unnamed)"
    return "".join(c if c.isprintable() else "?" for c in value)[:160]


def controller(command, yes, config, api, confirm=input):
    pod = api.get()
    if command == "endpoint":
        host, port = ssh_endpoint(pod)
        print(host)
        print(port)
    elif command == "status":
        print(f"Pod: {safe_name(pod.get('name'))}\nID: {config.pod_id}\nStatus: {pod['status']}")
        ready = pod["status"] == "RUNNING" and health_ready(config.local_port)
        print("Cloud model API: " + ("ready" if ready else "unavailable (normal when Pod is stopped)"))
    elif command == "start":
        if pod["status"] == "RUNNING":
            print("Pod already running. Inference readiness is not implied.")
            return 0
        if pod["status"] != "EXITED":
            raise ClientError("Refusing start in a transitional/unknown state; check status first")
        print(f"Current status: {pod['status']}\nStarting the Pod incurs GPU and storage charges.")
        if not yes:
            try:
                approved = confirm("Start RunPod? [yes/NO] ") == "yes"
            except EOFError:
                approved = False
            if not approved:
                print("Cancelled. Use start --yes only for deliberately approved automation.")
                return 0
        api.action("start")
        print("Start requested. Check runpod-bonsai status; server/tunnel are not started automatically.")
    elif command == "stop":
        if pod["status"] == "EXITED":
            print("Pod already stopped.")
            return 0
        api.action("stop")
        print("Stop requested. Confirm EXITED with runpod-bonsai status; acceptance alone is not shutdown.")
    else:
        raise ClientError("Unknown controller command", 2)
    return 0


def private_directory(path):
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ClientError("State/SSH directory must not traverse symlinks", 2)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ClientError("State/SSH directory must be owned by this user and mode 700", 2)


def prepare_ssh(config):
    private_file(config.key, limit=1024 * 1024)
    private_directory(config.known_hosts.parent)
    if config.known_hosts == config.key:
        raise ClientError("known_hosts must not be the private key", 2)
    try:
        fd = os.open(config.known_hosts, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        private_file(config.known_hosts, limit=1024 * 1024)
    else:
        os.close(fd)
    # Never truncate or clear a known host; endpoint rotation preserves its alias.


def child_environment():
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("RUNPOD_", "BONSAI_"))}


def ssh_arguments(config, endpoint, command=None, forward=False):
    host, port = endpoint
    # -o has its own syntax; quote a path containing spaces for OpenSSH's parser.
    known = str(config.known_hosts).replace("\\", "\\\\").replace('"', '\\"')
    args = ["ssh", "-F", "/dev/null", "-T", "-a", "-x", "-S", "none",
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
            "-o", "ForwardAgent=no", "-o", "ForwardX11=no",
            "-o", "ControlMaster=no", "-o", "PermitLocalCommand=no",
            "-o", f"ConnectTimeout={config.ssh_timeout}", "-o", "ConnectionAttempts=1",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes", "-o", f"StrictHostKeyChecking={config.host_policy}",
            "-o", f'UserKnownHostsFile="{known}"', "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", f"HostKeyAlias=runpod-{config.pod_id}",
            "-i", str(config.key), "-p", str(port)]
    if forward:
        args += ["-N", "-L", f"127.0.0.1:{config.local_port}:127.0.0.1:{config.remote_port}"]
    args += [f"{config.ssh_user}@{host}"]
    if command:
        # OpenSSH joins remote arguments into shell text; quote each one here.
        if any("\x00" in arg for arg in command):
            raise ClientError("NUL byte in remote command", 2)
        args.append(shlex.join(command))
    return args


def serve_command(config):
    return ["env", f"BONSAI_ROOT={config.remote_root}", f"BONSAI_PORT={config.remote_port}",
            str(PurePosixPath(config.remote_root) / "bin/bonsai-server-start")]


def run_remote(config, api, command):
    prepare_ssh(config)
    endpoint = ssh_endpoint(api.get())
    # Only SSH is invoked; credentials are not forwarded in argv or environment.
    return subprocess.run(ssh_arguments(config, endpoint, command),
                          env=child_environment(), check=False).returncode


class TunnelLock:
    def __init__(self, config):
        private_directory(config.state_dir)
        path = config.state_dir / f"tunnel-{config.local_port}.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        self.file = os.fdopen(fd, "r+")
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ClientError("Tunnel lock must be an owned private regular file", 2)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.file.close()
            raise

    def close(self):
        self.file.close()


def stop_child(process):
    # Only a child Popen owned by this invocation, never a persisted bare PID.
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def tunnel_loop(config, api, stop_event, popen=subprocess.Popen, clock=time.monotonic):
    """Foreground SSH supervision; refresh mapping while connected, no autossh cache."""
    process = None
    endpoint = None
    failures = 0
    stable_since = None
    next_refresh = 0.0
    try:
        while not stop_event.is_set():
            now = clock()
            if process is not None and process.poll() is not None:
                process.wait()
                process = None
                stable_since = None
                next_refresh = 0.0
                failures += 1
                print("SSH exited; re-resolving endpoint before reconnect", file=sys.stderr)
                if failures >= config.max_failures:
                    raise ClientError("Tunnel retry budget exhausted; inspect SSH diagnostics. Pod may still be billable.", 75)
                if stop_event.wait(config.retry_seconds):
                    break
            if process is None or now >= next_refresh:
                try:
                    pod = api.get()
                    if pod["status"] == "EXITED":
                        print("Pod is EXITED; stopping only the local SSH tunnel", file=sys.stderr)
                        return 0
                    current = ssh_endpoint(pod)
                except ClientError as exc:
                    stop_child(process)
                    process = None
                    stable_since = None
                    failures += 1
                    print(f"Tunnel discovery unavailable: {exc}", file=sys.stderr)
                    if failures >= config.max_failures:
                        raise ClientError("Tunnel discovery retry budget exhausted; Pod state/readiness unconfirmed", 75) from None
                    if stop_event.wait(config.retry_seconds):
                        break
                    continue
                if process is not None and current != endpoint:
                    print("SSH mapping changed; replacing only this managed connection", file=sys.stderr)
                    stop_child(process)
                    process = None
                if process is None:
                    endpoint = current
                    process = popen(ssh_arguments(config, current, forward=True),
                                    stdin=subprocess.DEVNULL, env=child_environment(), start_new_session=True)
                    stable_since = clock()
                    print(f"SSH launched for loopback port {config.local_port}; model readiness is separate", file=sys.stderr)
                elif stable_since is not None and now - stable_since >= 60:
                    failures = 0
                next_refresh = clock() + config.refresh_seconds
            stop_event.wait(min(0.5, max(0.05, next_refresh - clock())))
        return 0
    finally:
        stop_child(process)


def run_tunnel(config, api):
    prepare_ssh(config)
    try:
        lock = TunnelLock(config)
    except BlockingIOError:
        raise ClientError("Another managed tunnel owns this local port", 2) from None
    event = threading.Event()
    old = {}
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            old[sig] = signal.signal(sig, lambda _sig, _frame: event.set())
        return tunnel_loop(config, api, event)
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
        lock.close()


def inspect_pod(config, api):
    pod = api.get()
    # Launch args/env may contain secrets: report presence, never their values.
    data = {"id": config.pod_id, "name": safe_name(pod.get("name")), "status": pod["status"],
            "custom_launch_args_present": bool(pod.get("args")),
            "local_port": config.local_port, "remote_port": config.remote_port}
    if pod["status"] == "RUNNING":
        try:
            ip, port = ssh_endpoint(pod)
            data["ssh_endpoint"] = {"ip": ip, "port": port}
        except ClientError:
            data["ssh_endpoint"] = None
    print(json.dumps(data, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "endpoint", "stop", "serve", "tunnel", "inspect", "check-config"):
        sub.add_parser(name)
    start = sub.add_parser("start")
    start.add_argument("--yes", action="store_true", help="Explicit consent to start paid compute without a prompt")
    ssh = sub.add_parser("ssh")
    ssh.add_argument("remote_command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        config = Config.load()
        api = RunPodClient(config)
        if args.command in ("status", "endpoint", "start", "stop"):
            return controller(args.command, getattr(args, "yes", False), config, api)
        if args.command == "check-config":
            print("Configuration valid (credential values not displayed; no network request made).")
            return 0
        if args.command == "inspect":
            return inspect_pod(config, api)
        if args.command == "tunnel":
            return run_tunnel(config, api)
        if args.command == "serve":
            return run_remote(config, api, serve_command(config))
        command = args.remote_command
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            raise ClientError("Use runpod-ssh -- COMMAND [ARG ...]; interactive shells are not enabled", 2)
        return run_remote(config, api, command)
    except ClientError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.code
    except OSError:
        print("ERROR: local file/process operation failed; check installation and private permissions", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Check Pod status; local cancellation does not stop cloud billing.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
