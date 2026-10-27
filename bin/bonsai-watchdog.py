#!/usr/bin/env python3
"""Conservative RunPod inference watchdog. Dry-run by default.

Monitors llama-server /slots. An actively processing slot resets the idle
timer. A prolonged /slots outage triggers an independent unhealthy timeout.
Stopping the Pod is disabled unless --execute-stop is explicitly provided.
"""
import argparse
import json
import os
import subprocess
import time
import urllib.request
from datetime import datetime, timezone


def get_slot_state(url, timeout=5):
    """Return True when any llama-server slot is processing.

    Raises when /slots is absent, inaccessible, or returns an unexpected shape.
    """
    with urllib.request.urlopen(url, timeout=timeout) as response:
        data = json.load(response)
    if not isinstance(data, list) or not data:
        raise ValueError("Unexpected /slots payload (expected nonempty list)")
    if any(
        not isinstance(slot, dict)
        or not isinstance(slot.get("is_processing"), bool)
        for slot in data
    ):
        raise ValueError("Missing boolean is_processing in /slots")
    return any(slot["is_processing"] for slot in data)


class Watchdog:
    """State machine. Values and timestamps are monotonic seconds."""

    def __init__(self, idle_seconds, unhealthy_seconds, max_seconds, started_at):
        self.idle_seconds = idle_seconds
        self.unhealthy_seconds = unhealthy_seconds
        self.max_seconds = max_seconds
        self.started_at = started_at
        self.idle_since = started_at
        self.failed_since = None

    def observe(self, now, processing=None, error=False):
        if error:
            if self.failed_since is None:
                self.failed_since = now
            # Unknown server state must not count toward the idle timeout.
            self.idle_since = now
            state = "unhealthy"
        else:
            self.failed_since = None
            if processing:
                self.idle_since = now
                state = "processing"
            else:
                state = "idle"

        # Explicit hard cost ceiling; may interrupt an active generation.
        if now - self.started_at >= self.max_seconds:
            return state, "maximum-session-duration"
        if self.failed_since is not None:
            if now - self.failed_since >= self.unhealthy_seconds:
                return state, "prolonged-unhealthy-server"
        elif state == "idle" and now - self.idle_since >= self.idle_seconds:
            return state, "idle-timeout"
        return state, None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pod-id", default=os.getenv("RUNPOD_POD_ID", ""))
    parser.add_argument("--url", default="http://127.0.0.1:18080/slots")
    parser.add_argument("--idle-minutes", type=float, default=45)
    parser.add_argument("--unhealthy-minutes", type=float, default=90)
    parser.add_argument("--max-hours", type=float, default=8)
    parser.add_argument("--interval-seconds", type=float, default=30)
    parser.add_argument(
        "--execute-stop",
        action="store_true",
        help="Dangerous: actually stop Pod (dry-run is the default)",
    )
    parser.add_argument(
        "--stop-command",
        default="runpodctl",
        help="Path to validated runpodctl binary (used only with --execute-stop)",
    )
    args = parser.parse_args()
    if min(
        args.idle_minutes, args.unhealthy_minutes,
        args.max_hours, args.interval_seconds,
    ) <= 0:
        parser.error("All timeout values must be positive")
    if args.execute_stop:
        if not args.pod_id or not all(
            ch.isalnum() or ch in "-_" for ch in args.pod_id
        ):
            parser.error("A valid --pod-id is required with --execute-stop")
        if not os.path.isfile(args.stop_command) and "/" in args.stop_command:
            parser.error("Stop command path does not exist")

    started = time.monotonic()
    watchdog = Watchdog(
        args.idle_minutes * 60,
        args.unhealthy_minutes * 60,
        args.max_hours * 3600,
        started,
    )
    while True:
        now = time.monotonic()
        try:
            processing = get_slot_state(args.url)
            state, reason = watchdog.observe(now, processing=processing)
        except (OSError, ValueError, TimeoutError, json.JSONDecodeError) as exc:
            state, reason = watchdog.observe(now, error=True)
            print(f"probe_error={type(exc).__name__}: {exc}", flush=True)

        print(
            f"{datetime.now(timezone.utc).isoformat()} "
            f"state={state} action={reason or 'none'}",
            flush=True,
        )
        if reason:
            if not args.execute_stop:
                print(f"DRY-RUN: would stop Pod ({reason}).", flush=True)
                return
            print(f"STOP requested: {reason}", flush=True)
            subprocess.run(
                [args.stop_command, "pod", "stop", args.pod_id],
                check=True, timeout=45,
            )
            return
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
