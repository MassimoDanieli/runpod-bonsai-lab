#!/usr/bin/env python3
"""Conservative RunPod llama-server watchdog. DRY-RUN by default."""
import argparse
import json
import os
import subprocess
import time
import urllib.request
from datetime import datetime, timezone


def get_slot_state(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        data = json.load(response)
    if not isinstance(data, list) or not data:
        raise ValueError('Unexpected /slots payload')
    if any(not isinstance(slot, dict) or not isinstance(slot.get('is_processing'), bool) for slot in data):
        raise ValueError('Missing is_processing in /slots')
    return any(slot['is_processing'] for slot in data)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--pod-id', default=os.getenv('RUNPOD_POD_ID', ''))
    p.add_argument('--url', default='http://127.0.0.1:18080/slots')
    p.add_argument('--idle-minutes', type=float, default=45)
    p.add_argument('--unhealthy-minutes', type=float, default=90)
    p.add_argument('--max-hours', type=float, default=8)
    p.add_argument('--interval-seconds', type=int, default=30)
    p.add_argument('--execute-stop', action='store_true', help='Actually stop the Pod (default: dry-run)')
    args = p.parse_args()
    if min(args.idle_minutes, args.unhealthy_minutes, args.max_hours, args.interval_seconds) <= 0:
        p.error('All time settings must be positive')
    if args.execute_stop and (not args.pod_id or not all(c.isalnum() or c in '-_' for c in args.pod_id)):
        p.error('A valid --pod-id is required with --execute-stop')
    start = idle_since = time.monotonic()
    failed_since = None
    while True:
        now = time.monotonic()
        try:
            processing = get_slot_state(args.url)
            failed_since = None
            if processing:
                idle_since = now
                status = 'processing'
            else:
                status = 'idle'
        except Exception as exc:
            if failed_since is None:
                failed_since = now
            status = f'unhealthy ({type(exc).__name__}: {exc})'
            # Do not falsely count a failed request as proven idle.
            idle_since = now
        reason = None
        if now - start >= args.max_hours * 3600:
            reason = 'maximum-session-duration'
        elif failed_since is not None and now - failed_since >= args.unhealthy_minutes * 60:
            reason = 'prolonged-unhealthy-server'
        elif failed_since is None and status == 'idle' and now - idle_since >= args.idle_minutes * 60:
            reason = 'idle-timeout'
        print(f'{datetime.now(timezone.utc).isoformat()} {status} reason={reason or "none"}', flush=True)
        if reason:
            if not args.execute_stop:
                print('DRY-RUN: would stop pod; watchdog exiting.', flush=True)
                return
            command = ['runpodctl', 'pod', 'stop', args.pod_id]
            print(f'Stopping pod ({reason})', flush=True)
            subprocess.run(command, check=True, timeout=45)
            return
        time.sleep(args.interval_seconds)


if __name__ == '__main__':
    main()
