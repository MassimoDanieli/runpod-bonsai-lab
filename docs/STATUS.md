# Implementation and validation status

## Process-supervision branch, 27 October 2026

This branch is stacked on safety PR #2. It adds Pod-local process management for
review; no host/Pod deployment, boot-hook change, GPU start or real cloud stop has
been performed. Existing LOCAL inference is untouched.

New implementation:

- Linux lifetime locks and structured PID/start-time/boot/namespace/executable
  identity; pidfd-based external stop, refusing unmanaged listeners/processes.
- Detached supervisor with a separately initialized dry-run watchdog before the
  model starts. Readiness requires both `/health` and `/slots`, with a timeout.
- Model failure leaves the watchdog observing; watchdog failure stops the managed
  model and uses a bounded restart budget without resetting the session deadline.
- Explicit resume/new-session rules; no guess that a watchdog restart is a Pod start.
- Bounded live model/supervisor logs. Watchdog logs rotate at restart only, keeping
  their direct output descriptor independent of supervisor lifetime.
- Compatibility launchers start the managed pair; an optional systemd example
  applies only where systemd is actually available.
- New offline unit and process-integration checks are included in `tests/run-all.sh`.

See the PR for actual local and CI results; publication is not live deployment.
The supervisor deliberately refuses actual watchdog shutdown. It may stop local
inference on a dry-run decision, but **does not stop paid GPU billing**.

## Safety PR #2 retained

Private no-clobber backups, same-stream hashes, source-change detection, explicit
asset selection, contained relative library materialization, staged atomic Linux
restore, REST watchdog shutdown/reconciliation/backoff, persistent session records,
finite timeout validation, ignore rules and offline CI remain in this branch.
The existing safety tests must keep passing.

## Not implemented or not verified yet

- Genuine Pod-run identity supplied by a trusted lifecycle controller. Current
  supervisor session identity is local and resets only with explicit authorization.
- Live model/`--slots` compatibility and boot/restart tests for the actual RunPod
  PyTorch image. No `/post_start.sh` hook is installed.
- External recovery from whole-supervisor SIGKILL/OOM, container death, host/storage
  failure, and continuous watchdog log retention in a long-lived deployment.
- Real least-privilege REST shutdown from inside the Pod with controller offline.
- Common SSH/configuration refactor, endpoint refresh during reconnect,
  configurable remote root/local controller health port and noninteractive start.
- `up/down/wait`, automated replacement Pods, and portable storage strategy.
- Real restored-model inference, licence choice, complete secret/history audit,
  and an independent provider/account-level spending guard.

Retain the source Pod/data. Keep actual shutdown disabled until controlled live
acceptance. Do not merge or deploy solely because an offline test suite passes.
