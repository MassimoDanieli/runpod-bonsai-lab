# Pod-side watchdog

The watchdog runs separately from inference and is **dry-run by default**. Actual
shutdown requires `--execute-stop`, an explicit session ID, and a RunPod API
credential. The REST backend has offline regression coverage, not a completed
live Pod shutdown/restart acceptance test.

## Decision policy

| Condition | Default | Action |
| --- | --- | --- |
| All slots confirmed idle | 45 minutes | Recheck activity, then request stop |
| Any slot processing | Any time | Reset idle interval |
| Invalid/unreachable `/slots` | Less than 90 minutes | Unknown, not idle |
| Sustained invalid/unreachable `/slots` | 90 minutes | Recheck, then request stop |
| Hard session ceiling | 8 hours | Request stop even during active inference |

Idle time starts on the **first confirmed idle sample**, not the previous busy
sample. Polling can miss short requests; a final recheck narrows the race but is
not an atomic admission/drain lock. The hard ceiling is deliberately interrupting.
A missing or incompatible `/slots` endpoint is treated as unhealthy, so validate
the actual PrismML response before enabling real shutdown.

## Persistent session identity

The deployment launcher/operator must assign one stable `--session-id` per actual
Pod run, reusing it when only the watchdog restarts. Do not invent an API timestamp
field. With a session ID, the state file records the origin, elapsed high-water
mark and last observed wall time; a lock excludes concurrent watchdog writers.
A restart accounts for downtime using wall time and retains the elapsed maximum.
Large backward wall-clock jumps or corrupt state fail visibly rather than granting
a new budget. Reliable clock and storage remain requirements.

By default, the origin is the **first watchdog start**, not Pod allocation time.
Supply `--session-start-epoch` from a trusted launcher to include earlier runtime.
A different Pod run requires a different ID **and** explicit `--new-session` once.
The same ID cannot be reset by that flag. Never generate a fresh ID on every
watchdog restart. Session IDs and timestamps are not secrets, but keep state private.

```bash
python3 bin/bonsai-watchdog.py \
  --session-id example-run-001 \
  --state-file /workspace/ai/Bonsai-demo/run/watchdog-session.json \
  --idle-minutes 45 --unhealthy-minutes 90 --max-hours 8
```

Without a session ID, dry-run remains stateless for experimentation. Dry-run logs
its first would-stop decision and exits; it does not protect the account balance.
The example systemd unit now references the installed `bin/bonsai-watchdog.py`
and reads `BONSAI_SESSION_ID` from its private EnvironmentFile. It remains dry-run.
Most RunPod containers do not run systemd; this unit is not a verified boot hook.

## REST shutdown backend

`runpodctl` and `--stop-command` have been removed. The watchdog uses the same
verified control-plane endpoint as the controller:

- `GET https://api.runpod.io/v2/pods/{id}` to reconcile status and Pod identity.
- `POST .../{id}/action` with `{"action":"stop"}` to request shutdown.
- Only `EXITED` confirms shutdown. HTTP 2xx alone does not.

A timeout after an accepted stop is reconciled with GET before another POST.
Transient/authorization errors are redacted, logged and retried with bounded
backoff (5 seconds to 5 minutes) while the watchdog remains alive. Requests have
bounded timeouts, no automatic redirects and no implicit proxy. Permanent denied
credentials still require operator intervention; retries cannot repair permissions.
No replacement Pod is created and no resource is deleted.

Use a **separate, minimally scoped stop credential**, not the controller's broad
account-level token by default. `--api-key-file` accepts a raw token file owned by
the watchdog user with mode 600 or stricter (not a shell `.env` file). Alternatively,
use `RUNPOD_API_KEY` from a trusted service environment. Keys and response bodies
are not logged. Never place the key in Git, CLI arguments, or a model workspace.

For a supervised live acceptance test only, after verifying the session identity,
credential scope and `/slots`, add `--execute-stop --pod-id YOUR_POD_ID` and the
credential source. This PR does not deploy or enable that mode.

## Tests and remaining boundaries

Run `bash tests/run-all.sh` on Linux. It makes no real RunPod requests. Watchdog
tests cover malformed slots, timers, restart persistence, clock rollback, locking,
HTTP 401/403/429/5xx, ambiguous transport failure, fresh activity veto, dry-run,
retry and confirmed EXITED.

Still pending: independently supervised boot startup, a live shutdown test with
the controller disconnected, graceful traffic admission/drain, and an independent
provider/account budget guard. A dead watchdog or failed local filesystem cannot
be made safe by code that is no longer executing.

Reference: [RunPod Python client's REST actions](https://github.com/runpod/runpod-python/blob/main/runpod/api/ctl_commands.py).
