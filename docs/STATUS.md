# Implementation and validation status

## Safety-fix branch, 27 October 2026

This change set addresses backup/restore integrity and watchdog shutdown reliability
identified in review PR #1. It is not a deployment, boot integration or complete
`up/down/wait` orchestrator. No paid compute or real credential is used in tests.

Implemented and locally tested on Linux / Python 3.13.5:

- Private staged artifact backups; no deletion of competing final artifacts.
- Same-stream hashes, source-change detection, actual optional model names and
  contained relative library-link materialization.
- Staged, no-clobber restore; canonical paths, collision/resource checks and
  atomic Linux publication. Final destination must be absent.
- Watchdog REST stop, fresh activity gate, status reconciliation, bounded backoff,
  confirmed EXITED, finite time settings and private persistent session state.
- Watchdog unit path fixed. Dry-run remains the default.
- Offline test runner, CI definition and additional private-artifact ignore rules.

The unit suite contains 56 tests, supplemented by the existing GPU-unavailable,
startup-preflight and migration smoke scripts. Local execution results are recorded
in the PR. GitHub Actions is separate: do not claim its status until it actually runs.

## Not fixed or not verified yet

- Server/helper stale PID identity, locking, cold-start watchdog gap and logging.
- Automatic session identity creation tied to an actual Pod run.
- Live REST shutdown from inside the Pod with limited credentials and controller offline.
- Image boot hooks, SSH/Jupyter persistence and independent process supervision.
- Common SSH/configuration refactor, automatic endpoint refresh during reconnection,
  configurable remote root and local health port, and noninteractive start approval.
- Lifecycle orchestration, replacement-Pod policy and portable storage strategy.
- Runtime compatibility of a multi-gigabyte restored real model.
- Licence choice, broader secret/history audit, and provider-side budget guard.

Keep watchdog shutdown disabled until live acceptance tests are completed. Keep
the source Pod/data. Do not merge a branch merely because its unit tests pass.
The technical report documents the original lab, not every deployment's state.
