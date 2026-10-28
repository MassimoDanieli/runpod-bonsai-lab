# Implementation and validation status

## Modal serverless path, 28 October 2026

Added `cloud/` and [Modal guide](MODAL.md) as an alternative to the Pod for
occasional use. Repository code only: no Modal account, image build, GPU or
token was used.

- Checked offline: the PrismML release's SHA-256, its compiled GPU architectures
  (native sm_86/89/120, PTX only for sm_80/90), that every library except
  `libcuda.so.1` resolves from the pinned CUDA wheels, and that `--version` runs.
- The app loads with the Modal 1.6.3 SDK; `huggingface_hub==2.3.0` exposes the
  APIs used. 22 new offline tests, including static cost/exposure invariants.
- Not yet verified live: image build, cold-start time, tok/s on L40S, behaviour of
  long streams against Modal's 150 s web limit, and whether unauthenticated
  requests ever start a container. See the acceptance checklist in the guide.

## Lifecycle orchestration branch, 28 October 2026

Stacked on shared-client PR #4 (and therefore PRs #3/#2). This development changes
repository code only: no merge, deployment, live RunPod request, SSH connection,
GPU allocation, token copy, entrypoint modification or original-data deletion.

Implemented in this branch:

- `runpod-bonsai up/down/wait` with finite deadlines and unchanged low-level commands.
- Local preflight before paid start, explicit confirmation or --yes, one start/stop
  action per invocation, GET reconciliation after ambiguous action outcomes.
- Private persistent start intents, same-controller lifecycle locks, session origin
  propagation and no silent reset/adoption of a different active remote session.
- Remote supervisor readiness/liveness checks; loopback health is not enough alone.
- Dedicated transient systemd user tunnel service, ownership fingerprint, managed
  SSH listener check, no arbitrary PID kills and no unsupervised nohup fallback.
- Down continues the requested Pod stop after local cleanup failure; EXITED must
  be observed before confirming stopped state. No automatic rollback after up fails.
- 57 new offline tests plus the existing suites and scripts in tests/run-all.sh.

New local tests passed with simulated REST/SSH/systemd, private temporary files,
real file locks and a real loopback child/listener check. Runtime/test blobs were
matched to the locally tested versions. See the PR for actual CI results; no live
systemd user manager or restarted GPU Pod was exercised during development.

## Earlier changes retained

PR #4: shared private data-only config, REST client, SSH builder and endpoint-refresh
supervisor; configurable ports/root, explicit start approval and capacity errors.
PR #3: Linux process identities/locks, pidfds, model + dry-run watchdog, bounded
readiness/restarts, preserved sessions and model/supervisor log rotation.
PR #2: private no-clobber backups, safe staged restore, REST watchdog reconciliation,
private persistent session state and expanded offline/CI safety tests.

## Remaining acceptance and limitations

- Live systemd transient-unit creation, SSH handshakes, host-key/endpoint changes,
  real model/--slots behavior, and complete up -> inference -> down acceptance.
- Pod scripts/model/SSH must already be installed; no deployment or boot hook is
  installed by up. User-service lifetime after logout must be checked explicitly.
- Lifecycle origin is local start-request time, not verified GPU allocation time.
  External dashboard changes, multi-controller concurrency and lost state need review.
- Automatic real watchdog stop remains disabled by the Pod supervisor. Verify a
  separate least-privilege stop credential inside the Pod and a provider budget guard.
- Up failures and interruption do not stop the paid Pod automatically; use down.
  Down can report confirmed EXITED with a separate local-cleanup error.
- No unsupervised detached tunnel fallback, auto replacement Pod or storage migration.
- Recovery from OOM/SIGKILL/container/host/storage failure and continuous watchdog
  log retention require deployment-level supervision and tests.
- Restored real-model acceptance, licence selection and full secret/history audit.

Retain the source Pod/data and keep LOCAL independent. Offline tests and green CI
are review evidence, not authorization for unattended paid production deployment.
