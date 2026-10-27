# Pod-local process supervision (experimental)

This module addresses process ownership and cold startup. It does **not** start,
stop, recreate or configure a RunPod Pod, install a boot hook, or replace SSH/Jupyter.
The supervised watchdog is **dry-run only**. Stopping local inference is not a
provider-side GPU stop: continue to stop paid compute explicitly and confirm EXITED.

## Requirements and installation

Use Linux with `/proc`, pidfd support (Linux 5.4+), and Python 3.11+. A private local
runtime directory must support `flock`; do not assume this works on every network
filesystem. The account running the processes owns the directory and files.

On an accessible Pod, from a reviewed repository checkout, install **all** of these
files together (including the watchdog from the safety-fix branch):

```bash
export BONSAI_ROOT=/workspace/ai/Bonsai-demo
mkdir -p "$BONSAI_ROOT/bin"
install -m 755 bin/bonsai-pod.py bin/bonsai-watchdog.py \
  bin/bonsai-server-start bin/bonsai-pod-services-start "$BONSAI_ROOT/bin/"
```

The model and compatible CUDA server must already exist. The default supervisor
command enables `--slots`; verify that option on the deployed PrismML build before
relying on readiness. No download or GPU access occurs in the offline tests.

Existing `run/` directories created by the old scripts may have mode 755. Inspect
ownership first and then, **on the Pod only**, make your runtime directory private:

```bash
mkdir -p "$BONSAI_ROOT/run"
chmod 700 "$BONSAI_ROOT/run"
```

Do not run this installation against the local model's working directory during
review. These are Pod-side processes; the controller on the local PC is unchanged.

## Operations

```bash
python3 "$BONSAI_ROOT/bin/bonsai-pod.py" start
python3 "$BONSAI_ROOT/bin/bonsai-pod.py" status
python3 "$BONSAI_ROOT/bin/bonsai-pod.py" stop
```

`start` detaches a supervisor from the SSH terminal and waits a bounded time for
readiness. The compatibility commands `bonsai-server-start` and
`bonsai-pod-services-start` now both start the **supervised pair**, not a model-only
process. `bin/bonsai-server` remains a standalone foreground diagnostic launcher;
do not run it alongside the supervised pair.

`run` keeps the supervisor in the foreground for an external process manager.
`systemd/bonsai-pod.service` is an optional example for a systemd-capable host, not a
claim that a RunPod container has systemd as PID 1. Never install that unit and the
standalone watchdog unit together for the same runtime directory.

### Actual readiness

1. Validate paths, settings, locks and available loopback port. An existing unmanaged
   listener is **not** adopted and is never killed.
2. Establish/resume a private session record; initialize the dry-run watchdog first.
3. Wait for the watchdog's private pipe handshake, sent only after configuration
   and session locking succeeded. `/slots` does not need to exist yet.
4. Launch the model directly as a separate child with `--host 127.0.0.1`.
5. Report `READY` only while both children live and `/health` reports `status: ok`
   and `/slots` provides a nonempty list of boolean `is_processing` records.

The CLI distinguishes loading, ready, unhealthy, model failure, watchdog restart,
completed dry-run and stopped/stale/orphaned state. A cold model start no longer
prevents the watchdog from starting. If model startup fails or times out, start
returns nonzero; the supervisor and watchdog stay running to observe the failure.
The model is not repeatedly relaunched behind the operator's back.

## Failure and stop policy

| Condition | Behaviour |
| --- | --- |
| Concurrent starts | Private lifetime locks prevent duplicate supervisors. |
| Old bare `.pid` files | Ignored; they are not proof of process ownership. |
| Managed child exits | Model failure is reported while the watchdog continues. |
| Watchdog exits nonzero | Stop the managed model, retry the watchdog with bounded backoff, preserving the same session. The model remains stopped after recovery. |
| Watchdog completes its dry-run decision | Stop the managed model locally, then exit. **The Pod is not stopped and can still be billed.** |
| Watchdog initialization/restart budget fails | Do not leave the managed model running; report failure. No cloud stop is claimed. |
| Supervisor killed abruptly | Status identifies matching orphaned workers; refuse duplicate starts. Explicit `stop` targets only verified identities. External supervision is still required for unattended recovery. |

PID ownership includes process start ticks, boot ID, PID namespace, executable
identity, UID and command-line digest. External stop uses a Linux pidfd and rechecks
that fingerprint before signalling; it never uses an unverified numeric-PID kill.
Persisted state is not a security boundary against malicious code running as the
same user/root. No automatic adoption of arbitrary pre-existing processes occurs.

The watchdog's output goes to a file rather than a supervisor-owned pipe, so it
can continue after supervisor SIGKILL. Model stdout is streamed through the
supervisor for bounded logs; abrupt supervisor death can interrupt that output.
`setsid`/detachment alone does not supply crash recovery. An external manager or a
verified Pod boot integration remains necessary, including protection against
resource exhaustion and entire-container failure.

## Session/deadline handling

The first supervised run creates a session UUID and a persistent origin. Restarting
only the watchdog or supervisor reuses them; it does **not** reset the hard ceiling.
Even after the Pod stops/resumes, an old session is retained until explicitly changed.
This is conservative: the script does not infer a new billable session from an
unverified API timestamp or a new process PID.

After confirming a genuinely new Pod run, authorize a different identifier:

```bash
python3 "$BONSAI_ROOT/bin/bonsai-pod.py" start \
  --session-id 'operator-confirmed-run-002' --new-session
```

Do not do this for a watchdog retry. No reset is permitted while the supervisor is
active. `--session-start-epoch` can include an earlier known Pod startup time. The
default is the first supervisor launch, **not** provider allocation time. Corrupt
state is rejected, and an already-expired ceiling prevents another model launch.
Lifecycle orchestration must eventually provide and validate a genuine Pod-run ID.

## Configuration

Configuration is via explicit environment variables, not the controller's secret
shell configuration. Defaults are relative to the selected root/current script.

| Variable | Default / meaning |
| --- | --- |
| `BONSAI_ROOT` | `/workspace/ai/Bonsai-demo` |
| `BONSAI_STATE_DIR` | `$BONSAI_ROOT/run`; owned, mode 700, no symlinks |
| `BONSAI_SERVER_BIN` | `$BONSAI_ROOT/bin/cuda/llama-server` |
| `BONSAI_MODEL_PATH` | Primary PQ2_0 model under `$BONSAI_ROOT/models/bonsai2-gguf/27B` |
| `BONSAI_WATCHDOG` | `bonsai-watchdog.py` beside `bonsai-pod.py` |
| `BONSAI_PORT`, `BONSAI_CTX`, `BONSAI_NGL` | `18080`, `65536`, `99` |
| `BONSAI_READY_TIMEOUT` | 180 seconds; a bounded initial readiness wait |
| `BONSAI_POLL_SECONDS`, `BONSAI_PROBE_TIMEOUT` | 1 and 2 seconds |
| `BONSAI_IDLE_MINUTES`, `BONSAI_UNHEALTHY_MINUTES`, `BONSAI_MAX_HOURS` | 45, 90, 8; same watchdog policy as before |
| `BONSAI_WATCHDOG_INTERVAL`, `BONSAI_WATCHDOG_INIT_TIMEOUT` | 30 and 10 seconds |
| `BONSAI_WATCHDOG_RESTARTS`, `BONSAI_RESTART_DELAY` | 3 retries, initial 1 second, bounded exponential delay |
| `BONSAI_LOG_BYTES` | 10 MiB per live model/supervisor log, two rotated backups |
| `BONSAI_WATCHDOG_DRY_RUN` | Must be `1`; actual cloud shutdown is refused by this launcher |

`RUNPOD_POD_ID` identifies persisted state but is not used to call the API. The
supervised children receive an allowlisted environment without the controller's
RunPod API key or Hugging Face token. Supply only trusted executable/script paths.

Logs: `server.log`, `watchdog.log`, `supervisor.log`, and startup-only `launch.log`.
Model/supervisor logs rotate while written. Watchdog logs rotate **on restart**,
not continuously: the direct file descriptor deliberately survives the supervisor.
Monitor disk use during long sessions or arrange external copy/truncate retention
with an explicit acceptance of possible log loss. Logs are private and may include
model/prompt content; do not publish them blindly.

## Boot-hook inspection remains separate

The original `runpod/pytorch:1.4.1-cu1281-torch280-ubuntu2404` template has not yet
passed a live boot/restart acceptance test for this supervisor. No image, args,
`/start.sh`, `/post_start.sh`, SSH or Jupyter settings are changed by these scripts.

`bin/runpod-startup-preflight` is read-only text inspection, not a shell parser or
authorization to replace a hook. A reference in `/start.sh` does not prove invocation
order, blocking behaviour or persistence of root-level files. Inspect the actual
image before enabling any integration. `bin/runpod-inspect` reads declared settings;
its launch `args` can contain secrets even though it omits the `env` object.

## Offline acceptance

```bash
python3 -m unittest discover -s tests -p test_pod_supervisor.py -v
bash tests/run-all.sh
```

Fixtures use real Linux child processes and a synthetic loopback HTTP service, never
CUDA/model inference or the RunPod API. They cover cold load, model exit, watchdog
initialization/crash/retry, concurrent start, stale PID identity, orphan recovery,
private files, log limits, session retention and default refusal of real cloud stops.
These checks do not establish live GPU compatibility or safe unattended billing.

Implementation references: Python `subprocess` (`start_new_session`, direct child
management), Linux `proc_pid_stat(5)` (start time), `pidfd_send_signal(2)` (PID-reuse
protection). Review deployment-specific kernel and filesystem support.
