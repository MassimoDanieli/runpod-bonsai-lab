# Existing-Pod lifecycle orchestration

This adds `runpod-bonsai up`, `down` and `wait` on top of the shared client and
Pod-local supervisor. It does not create, migrate, terminate or delete Pods,
install model files, edit an entrypoint, enable real watchdog shutdown or modify
local inference. Only an explicitly approved `up` can request paid compute.

## Prerequisites and installation

Use a Linux controller with Python 3.11+, OpenSSH and a working **systemd user
manager** (`systemctl --user`, `systemd-run`). The orchestrator uses a dedicated
transient user service, not the legacy `runpod-tunnel.service`, and does not enable
a service at boot. There is deliberately **no unsupervised nohup fallback**.
Lower-level `start`, `stop`, `status`, `serve`, `ssh` and foreground `tunnel` remain
available independently. After logout, user-service lifetime depends on the host's
login/linger policy; verify it rather than changing it automatically.

Install the matching shared client, lifecycle module and wrappers together:

```bash
mkdir -p "$HOME/.local/bin"
install -m 700 bin/runpod-client.py bin/runpod-lifecycle.py \
  bin/runpod-bonsai bin/runpod-serve bin/runpod-tunnel bin/runpod-ssh \
  bin/runpod-inspect "$HOME/.local/bin/"
```

Keep the private `runpod.env` from [Configuration](CONFIGURATION.md). If the Pod workspace ignores `chmod`, set `BONSAI_REMOTE_STATE_DIR=/root/bonsai-state` after verifying that path has mode 700. `/root` may be ephemeral across Pod recreation; this is a temporary workaround, not durable session or billing accounting. Verify a private persistent filesystem before relying on state across Pod replacement. Existing
private settings are never overwritten. The RunPod Pod must already have the
matching `bonsai-pod.py`, `bonsai-watchdog.py`, model and CUDA runtime installed
under `BONSAI_REMOTE_ROOT`; consult [Pod startup](POD_STARTUP.md). SSH public-key
authentication and host identity must be established. This command does not deploy
the files or copy the controller API key to the Pod.

Stop an old/manual local tunnel explicitly before first use. An occupied local
port or an unrecognised service is refused, never adopted or killed. LOCAL's port
is not changed. First use of the per-Pod host-key alias remains TOFU with
`accept-new`; use pre-pinned keys and policy `yes` where appropriate.

## Commands

```bash
runpod-bonsai up                       # interactive consent if Pod is EXITED
runpod-bonsai up --yes --timeout 600    # explicit consent for unattended invocation
runpod-bonsai wait --state RUNNING --timeout 120
runpod-bonsai wait --state EXITED --timeout 120
runpod-bonsai down --timeout 180
```

`--timeout` (1..3600 seconds; default 600) bounds the operation's wait budget.
Subprocess deadlines and HTTP socket timeouts use the remaining budget. A single
in-flight OS/network operation can slightly overrun the polling deadline; this is
not a real-time execution guarantee. `wait` polls **Pod state only**; it does not
start compute, establish SSH, launch a model or establish inference readiness.
Read/API errors fail closed rather than silently interpreting them as a state.

## Up: phases and idempotency

1. Check local tools, private SSH files, user manager and local port ownership.
2. Read Pod state. For EXITED, ask for consent (or require `--yes`) and record a
   private start intent **before** one start POST. Preserve exit code 3 for
   `GPU_UNAVAILABLE`; never create a replacement or retry the paid action.
3. Wait for RUNNING, then authenticated SSH. Re-read the direct TCP mapping.
4. Read the remote supervisor's structured status. Reuse a ready/loading owned
   supervisor, rather than duplicate it. An inactive session is not silently
   reset. Failed/expired/orphaned active sessions require operator review.
5. Start the remote model + independent **dry-run** watchdog if absent, then
   require `ready` and live identities for both children.
6. Start/reuse a dedicated transient tunnel service. Reuse the shared tunnel's
   endpoint refresh and host-key checking. Require an SSH child of the managed
   unit to own the loopback listener, not merely any server returning HTTP 200.
7. Verify tunnel `/health` and recheck remote supervisor and RUNNING state.

Repeat `up` after a partial failure: it checks what is already running and does
not duplicate the model or tunnel. It never restarts an active expired session.
Readiness checks reduce races; they cannot guarantee the service remains healthy
after the command returns, nor replace a subsequent real inference test.

### Session origin

A paid start initiated here has a UUID and local request timestamp persisted on
the controller and supplied to the remote supervisor. That intent survives a CLI
restart. A known new paid run may explicitly replace a different **inactive**
remote session; an active different session is refused. Starting a model on a Pod
already running outside this orchestrator preserves the remote session contract:
it does not invent a provider allocation timestamp or reset its existing budget.
The timestamp is an observed local start-request time, **not a validated RunPod
billing timestamp**. External dashboard changes and lost state still require review.

## Failures and cost policy

There is **no automatic cloud rollback on `up` failure or Ctrl+C**. Stopping a Pod
that was already running could interrupt another workload, and an API timeout can
mean the start was accepted. The CLI reports the failure; inspect status and use
`down` for an intentional stop. A new, uncertain start is retained in the journal.
A repeat `up` will not send another start while that record is unresolved. If it
is still EXITED, inspect the provider and use `down` to acknowledge that state
before approving a later new start. The old low-level `start` remains explicit
but does not participate in this journal; do not interleave lifecycle tools or
external dashboard mutations during an operation.

`down` attempts to stop only its own transient local service and then requests
Pod stop. **A tunnel cleanup error does not block the cloud stop attempt.** It
polls until EXITED before reporting that state. A lost stop response is reconciled
through reads, not blindly repeated POSTs. If the deadline expires, no stopped
state is claimed; check the dashboard/status and retry the explicit stop as needed.
A corrupt local journal does not prevent a requested stop, and is not overwritten.
Persistent storage may remain billable after EXITED.

The watchdog remains dry-run. Successful `up` is not unattended billing
protection. Controller loss, suspended hosts, token revocation, unavailable GPU,
provider faults and exhausted credit need independent account/provider controls.

## Transient service details

The service name is `runpod-bonsai-<Pod-ID-hash>-<local-port>.service`. Its description
contains a fingerprint of non-secret configuration and installation paths. Units
with different fingerprints are refused; changing config while one is active
requires deliberate cleanup using the prior configuration. The manager owns the
SSH process group; no stored bare PID is signalled. `Restart=no` avoids multiplying
the shared tunnel's retry budget. Inspect `systemctl --user list-units 'runpod-bonsai-*'`
and the matching user journal. No API key is placed in service arguments or copied
to the remote environment; the client reads the private controller file.

This version deliberately requires systemd for `up`; it does not install lingering,
modify a permanent unit or keep working through a dead user service manager.
Local locks coordinate this configuration on one controller, not concurrent
operators on different computers. State storage must be private and trustworthy.

## Validation and remaining acceptance

```bash
python3 -m unittest discover -s tests -p test_lifecycle.py -v
bash tests/run-all.sh
```

The new suite uses fake REST/SSH/systemd operations, real private file locks and a
real local child/listener ownership check. No GPU or paid API call is used.
It covers repeated up/down, approval/EOF, unknown action outcomes, capacity errors,
readiness and timeout failures, preserving sessions, foreign unit/port rejection,
corrupted state, secret filtering and cleanup failure followed by a Pod stop.
A real systemd user-unit start, SSH handshake and restarted RunPod GPU cycle are
still live acceptance tests, not something these mocks prove.

Reference for transient services: https://www.freedesktop.org/software/systemd/man/255/systemd-run.html
