# Pod-side watchdog (experimental)

The watchdog is designed to run **inside the RunPod Pod**, independently of `llama-server`. It examines `http://127.0.0.1:18080/slots` and tracks three independent thresholds:

- **Idle** (default 45 minutes): all slots report `is_processing: false`; a busy slot resets the idle countdown.
- **Unhealthy** (default 90 minutes): the `/slots` endpoint is consistently unavailable or invalid; transient failures do not stop the Pod.
- **Maximum session** (default 8 hours): a strict cost ceiling that **can interrupt active inference**. Tune with care.

**Default: dry-run**. When a threshold is exceeded, the process logs its intended action and exits. No Pod shutdown occurs unless `--execute-stop` is explicitly selected.

## Offline tests

From the repository root, on any machine with Python 3:

```bash
python3 -m unittest discover -s tests -p 'test_watchdog.py' -v
```

Tests exercise healthy /slots payloads, idle timeout, active generations, transient and sustained failures, and the maximum-session cutoff. Tests do not require a GPU or API credentials.

## Pod dry-run example

After copying the script to a Pod with Python:

```bash
python3 bin/bonsai-watchdog.py --idle-minutes 45 --unhealthy-minutes 90 --max-hours 8
```

Do not assume that /slots is enabled in every PrismML llama-server build. First run `curl --fail http://127.0.0.1:18080/slots`, inspect the JSON and check whether a server option is required to enable it.

## Production blockers

Do **not** use `--execute-stop` until all of these are resolved:

1. Validate the exact stop command and its credentials **inside** the Pod, including stop capability while the controller host is disconnected. The current script calls `runpodctl pod stop`; RunPod templates do not necessarily include or authenticate that CLI.
2. Run the watchdog as an independently supervised process that starts on every Pod boot; a separate script launched manually with `nohup` is not a durable restart policy.
3. Verify the maximum runtime countdown across Pod restarts and the behavior of false-negative /slots results.
4. Test fail-safe behavior after multiple unexpected HTTP responses, a llama-server crash and a lost external connection.
5. Decide whether the session ceiling may interrupt active jobs and add a warning/deferral policy if required.
6. Establish a secondary budget guard outside the Pod.

The local controller and RunPod inference are unaffected by these dry-run scripts. Never commit API keys or other secret values.
