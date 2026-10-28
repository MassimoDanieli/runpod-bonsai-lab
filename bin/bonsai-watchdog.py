#!/usr/bin/env python3
"""Pod-side cost watchdog: dry-run by default, explicit REST stop opt-in.

The hard session ceiling may interrupt active inference. Session state survives
watchdog restarts; only an explicitly authorized new session resets its origin.
"""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API_BASE = "https://api.runpod.io/v2/pods"
BODY_LIMIT = 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_request(request, timeout=5):
    # Direct requests: no redirects carrying credentials and no implicit proxy.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(request, timeout=timeout)


def get_slot_state(url, timeout=5):
    with open_request(url, timeout) as response:
        raw = response.read(BODY_LIMIT + 1)
    if len(raw) > BODY_LIMIT:
        raise ValueError("Oversized /slots response")
    slots = json.loads(raw)
    if not isinstance(slots, list) or not slots:
        raise ValueError("Expected a nonempty /slots list")
    if any(not isinstance(item, dict) or type(item.get("is_processing")) is not bool for item in slots):
        raise ValueError("Missing boolean is_processing")
    return any(item["is_processing"] for item in slots)


class Watchdog:
    """Elapsed time uses monotonic seconds. Unknown state is never proven idle."""
    def __init__(self, idle_seconds, unhealthy_seconds, max_seconds, started_at):
        if any(not math.isfinite(v) or v <= 0 for v in (idle_seconds, unhealthy_seconds, max_seconds)):
            raise ValueError("Timeouts must be finite and positive")
        self.idle_seconds = idle_seconds
        self.unhealthy_seconds = unhealthy_seconds
        self.max_seconds = max_seconds
        self.started_at = started_at
        self.idle_since = None
        self.failed_since = None

    def observe(self, now, processing=None, error=False):
        if error:
            self.idle_since = None
            if self.failed_since is None:
                self.failed_since = now
            state = "unhealthy"
        else:
            if type(processing) is not bool:
                raise ValueError("processing must be a boolean")
            self.failed_since = None
            if processing:
                self.idle_since = None
                state = "processing"
            else:
                if self.idle_since is None:
                    self.idle_since = now
                state = "idle"
        if now - self.started_at >= self.max_seconds:
            return state, "maximum-session-duration"
        if self.failed_since is not None and now - self.failed_since >= self.unhealthy_seconds:
            return state, "prolonged-unhealthy-server"
        if self.idle_since is not None and now - self.idle_since >= self.idle_seconds:
            return state, "idle-timeout"
        return state, None


class SessionRecord:
    """Persistent run origin plus elapsed high-water mark; one writer via flock.

    session_id is supplied by a trusted operator/launcher, not guessed from an
    unverified API field. The default origin is first watchdog launch, not GPU
    allocation time. Supply --session-start-epoch to include earlier runtime.
    """
    def __init__(self, path, pod_id, session_id, now_wall, new_session=False, start_epoch=None):
        self.path = Path(path).expanduser().absolute()
        self.lock = None
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", session_id):
            raise ValueError("Invalid session ID")
        if any(parent.is_symlink() for parent in self.path.parents) or self.path.is_symlink():
            raise ValueError("State path must not use symlinks")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = Path(str(self.path) + ".lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self.lock = os.fdopen(fd, "r+")
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError("Lock must be an owned private regular file")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            previous = None
            if self.path.exists():
                previous = json.loads(read_private_file(self.path))
                if not isinstance(previous, dict) or previous.get("format") != 1:
                    raise ValueError("Invalid session state; refusing to reset it")
                for key in ("started_at", "last_wall", "elapsed_seconds"):
                    value = previous.get(key)
                    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                        raise ValueError("Invalid session timestamp")
                same = previous.get("pod_id") == pod_id and previous.get("session_id") == session_id
                if same:
                    if new_session:
                        raise ValueError("A reset requires a DIFFERENT session ID")
                    if now_wall < previous["last_wall"] - 30:
                        raise ValueError("Wall clock moved backwards; refusing to extend session")
                    self.data = previous
                elif not new_session:
                    raise ValueError("Session changed; explicitly authorize --new-session")
            if previous is None or new_session:
                origin = now_wall if start_epoch is None else start_epoch
                if not math.isfinite(origin) or origin < 0 or origin > now_wall:
                    raise ValueError("Session origin must be finite and not in the future")
                self.data = {"format": 1, "pod_id": pod_id, "session_id": session_id,
                             "started_at": origin, "elapsed_seconds": now_wall - origin,
                             "last_wall": now_wall}
            self.elapsed = max(self.data["elapsed_seconds"], now_wall - self.data["started_at"])
            self.checkpoint(self.elapsed, now_wall)
        except BaseException:
            self.close()
            raise

    def checkpoint(self, elapsed, now_wall):
        self.data["elapsed_seconds"] = max(self.data["elapsed_seconds"], elapsed)
        self.data["last_wall"] = max(self.data["last_wall"], now_wall)
        fd, name = tempfile.mkstemp(prefix=".session-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(self.data, handle)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def close(self):
        if self.lock is not None:
            self.lock.close()
            self.lock = None


def read_private_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("Credential/state file must be owned and mode 600 or stricter")
        raw = handle.read(16385)
    if len(raw) > 16384:
        raise ValueError("Private file is too large")
    return raw.decode("utf-8")


class APIError(Exception):
    """Safe diagnostic: never includes request headers, keys or response bodies."""


class RunPodAPI:
    def __init__(self, pod_id, key, timeout=15):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", pod_id):
            raise ValueError("Invalid Pod ID")
        if not key or any(c.isspace() for c in key) or len(key) > 4096:
            raise ValueError("Invalid API credential")
        self.pod_id = pod_id
        self.url = f"{API_BASE}/{pod_id}"
        self.key = key
        self.timeout = timeout

    def request(self, method, action=False):
        req = urllib.request.Request(
            self.url + ("/action" if action else ""),
            data=b'{"action":"stop"}' if action else None,
            method=method,
            headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json"},
        )
        try:
            with open_request(req, self.timeout) as response:
                if not 200 <= response.status < 300:
                    raise APIError(f"RunPod {method}: HTTP {response.status}")
                raw = response.read(BODY_LIMIT + 1)
        except urllib.error.HTTPError as exc:
            raise APIError(f"RunPod {method}: HTTP {exc.code}") from None
        except (OSError, urllib.error.URLError, TimeoutError):
            raise APIError(f"RunPod {method}: transport failure; state not confirmed") from None
        if len(raw) > BODY_LIMIT:
            raise APIError("RunPod response exceeds limit")
        if action:
            return None  # Acceptance is NOT confirmation; the next GET reconciles.
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise APIError("RunPod returned invalid JSON") from None
        if (not isinstance(data, dict) or data.get("id") != self.pod_id
                or not isinstance(data.get("status"), str) or not data["status"]):
            raise APIError("RunPod returned an unexpected Pod/status")
        return data["status"]

    def status(self):
        return self.request("GET")

    def stop(self):
        self.request("POST", action=True)


def shutdown_step(api, reason, gate, settling=False):
    """Reconcile first, recheck activity, POST once; caller retries with backoff."""
    status = api.status()
    if status == "EXITED":
        return "confirmed"
    if status != "RUNNING":
        return "pending"
    # Hard cap is intentionally interrupting. Idle/unhealthy must requalify.
    if reason != "maximum-session-duration" and not gate():
        return "pending" if settling else "cancelled"
    api.stop()
    return "accepted"


def log(message):
    print(f"{datetime.now(timezone.utc).isoformat()} {message}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pod-id", default=os.getenv("RUNPOD_POD_ID", ""))
    parser.add_argument("--url", default="http://127.0.0.1:18080/slots")
    parser.add_argument("--idle-minutes", type=float, default=45)
    parser.add_argument("--unhealthy-minutes", type=float, default=90)
    parser.add_argument("--max-hours", type=float, default=8)
    parser.add_argument("--interval-seconds", type=float, default=30)
    parser.add_argument("--execute-stop", action="store_true")
    parser.add_argument("--api-key-file", type=Path, help="Raw API key file, mode 600; not a shell .env file")
    parser.add_argument("--state-file", type=Path, default=Path(__file__).resolve().parents[1] / "run/watchdog-session.json")
    parser.add_argument("--session-id", default=os.getenv("BONSAI_SESSION_ID", ""))
    parser.add_argument("--session-start-epoch", type=float)
    parser.add_argument("--new-session", action="store_true", help="Explicitly replace a previous DIFFERENT session record")
    args = parser.parse_args()
    for value in (args.idle_minutes, args.unhealthy_minutes, args.max_hours, args.interval_seconds):
        if not math.isfinite(value) or value <= 0:
            parser.error("Timeouts must be finite and positive")
    url = urllib.parse.urlsplit(args.url)
    if url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost", "::1") or url.username or url.password:
        parser.error("/slots must use an unauthenticated local loopback HTTP endpoint")
    if args.execute_stop and not args.session_id:
        parser.error("--execute-stop requires an explicit stable --session-id")
    if args.new_session and not args.session_id:
        parser.error("--new-session requires --session-id")
    api = None
    record = None
    try:
        if args.execute_stop:
            key = read_private_file(args.api_key_file).strip() if args.api_key_file else os.getenv("RUNPOD_API_KEY", "")
            api = RunPodAPI(args.pod_id, key)
        if args.session_id:
            record = SessionRecord(args.state_file, args.pod_id, args.session_id, time.time(),
                                   args.new_session, args.session_start_epoch)
        elif args.session_start_epoch is not None:
            parser.error("--session-start-epoch requires --session-id")
        started = time.monotonic() - (record.elapsed if record else 0)
        watchdog = Watchdog(args.idle_minutes * 60, args.unhealthy_minutes * 60, args.max_hours * 3600, started)
        pending_reason = None
        settling = False
        retry_at = 0.0
        backoff = 5.0

        def probe():
            try:
                busy = get_slot_state(args.url)
                return watchdog.observe(time.monotonic(), processing=busy)
            except (OSError, ValueError, urllib.error.URLError, TimeoutError):
                log("probe unavailable or invalid; not counted as proven idle")
                return watchdog.observe(time.monotonic(), error=True)

        while True:
            state, reason = probe()
            now = time.monotonic()
            if record:
                record.checkpoint(now - started, time.time())
            log(f"state={state} action={reason or pending_reason or 'none'}")
            decision = reason or pending_reason
            if decision:
                if not args.execute_stop:
                    log(f"DRY-RUN: would stop Pod ({decision}); no API call made")
                    return 0
                if now >= retry_at:
                    # If a timed-out POST actually stopped the Pod, the next GET
                    # confirms EXITED without sending another stop request.
                    try:
                        outcome = shutdown_step(api, decision, lambda: probe()[1] is not None, settling)
                    except APIError as exc:
                        log(f"Stop not confirmed: {exc}; retrying with bounded backoff")
                        outcome = "pending"
                        settling = True
                    if outcome == "confirmed":
                        log("Pod status EXITED confirmed")
                        return 0
                    if outcome == "cancelled":
                        pending_reason = None
                        settling = False
                        backoff = 5.0
                        retry_at = 0.0
                        log("Shutdown cancelled after fresh activity check")
                    else:
                        pending_reason = decision
                        settling = settling or outcome == "accepted"
                        retry_at = time.monotonic() + backoff
                        backoff = min(backoff * 2, 300.0)
                        log("Awaiting EXITED; API acceptance alone is not shutdown confirmation")
            delay = args.interval_seconds
            if pending_reason:
                delay = min(delay, max(0.1, retry_at - time.monotonic()))
            time.sleep(delay)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Watchdog configuration/state error: {exc}\nNo confirmed shutdown. Operator attention required.\n")
    finally:
        if record:
            record.close()


if __name__ == "__main__":
    raise SystemExit(main())
