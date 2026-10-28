# Implementation and validation status

## Shared controller/configuration/SSH branch, 27 October 2026

Stacked on process-supervision PR #3, which depends on safety PR #2. No automatic
merge, installation on a host, live RunPod action, GPU launch, credential change or
cloud data deletion is performed by this development work.

Implemented in this branch:

- One data-only private config parser, REST client and SSH argument builder, shared
  by five compatibility entrypoints. Configurable local/remote ports and remote root.
- Explicit `start --yes`, safe EOF cancellation, strict Pod identity/port validation,
  preserved GPU_UNAVAILABLE exit 3, no automatic paid-action retries/provisioning.
- API credentials removed from SSH argv/environment; API errors/launch args redacted.
- Literal remote argument quoting, stable per-Pod host-key alias, strict/accept-new
  policy only, preserved known_hosts and noninteractive public-key authentication.
- Foreground direct-SSH supervisor refreshes mappings while connected and after
  disconnection, with loopback-only forwarding, private lifetime lock, bounded
  reconnects and cleanup of its owned child. No cached-address autossh reconnects.
- Generic configuration sample; unused fixed-endpoint tunnel example removed.
- Updated user unit, installation instructions and offline regression checks.

The implementation is tested using synthetic config, mocked HTTPS/SSH, a fake local
SSH executable and deterministic time. Actual local/CI outcomes are recorded in the
PR. This is not proof of live provider compatibility or unattended readiness.

## Earlier changes retained

PR #3 adds Linux process identity/locks, pidfd-based local stop, detached supervised
model and dry-run watchdog, initialization before loading, readiness checks, bounded
watchdog retries, session preservation and model/supervisor log retention.
PR #2 adds private no-clobber backups, integrity/containment checks, staged atomic
Linux restore, REST watchdog shutdown/reconciliation and persistent session records.
These prior suites remain in the offline test gate without reduced coverage.

## Not implemented or not verified yet

- Actual RunPod stop/start, endpoint and host-key rotation acceptance for this client.
- Genuine Pod-run identity supplied by a trusted lifecycle controller. Current
  supervisor session identity is local and resets only with explicit authorization.
- Live model/--slots compatibility and boot/restart behavior of the RunPod image.
  No /post_start.sh hook is installed by this branch.
- External recovery from whole-supervisor SIGKILL/OOM, container/host/storage failure;
  watchdog log rotation during continuous long-lived operation.
- Real least-privilege REST shutdown from the Pod with the controller offline.
- Complete up/down/wait orchestration, automatic replacement Pods and portable storage.
- Restored real-model acceptance, licence selection, full secret/history audit and
  independent provider/account budget protection.

Keep actual watchdog shutdown disabled, retain the original Pod/data, and do not
deploy solely because unit/CI tests pass. LOCAL inference remains independent.
