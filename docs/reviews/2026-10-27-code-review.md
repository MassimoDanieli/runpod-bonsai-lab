# Code review: RunPod Bonsai Lab

**Date:** 27 October 2026  
**Reviewed snapshot:** `c76cf1fdc3bd88f2564a60c67cee0d21dd854506`  
**Review scope:** the 11 runtime scripts in `bin/`, four test entrypoints, configuration examples, systemd templates and relevant operational documentation.  
**Change scope:** this review branch adds documentation and offline reproductions only. It does not change production scripts, install anything on a host, invoke RunPod, start/stop a Pod, or select a software licence.

## Executive assessment

The manual local/cloud integration is useful and the code has sensible defaults: loopback-only model serving, explicit confirmation before starting paid compute, dry-run watchdog operation, no automatic replacement Pod on `GPU_UNAVAILABLE`, and checksum verification before restore output is written.

However, **passing the current tests does not establish unattended operational safety**. There are reproducible gaps in shutdown failure handling, process identity, configuration propagation and backup integrity. Keep the watchdog in dry-run, retain the source Pod/data, and do not rely on these backup scripts as the sole recovery copy until the relevant findings are fixed.

The external review supplied by the repository owner is broadly sound. Its portability, CI and orchestration suggestions are useful, but safety fixes should precede cosmetic restructuring and a new `up` command.

## What was actually checked

The existing offline checks were run against source bytes matched to the GitHub blob hashes of the reviewed snapshot, not against reconstructed or modified implementations:

| Check | Outcome |
| --- | --- |
| Bash syntax for the shell scripts and shell tests | PASS |
| `python3 -m unittest discover -s tests -p test_watchdog.py -v` | 8 tests PASS |
| `bash tests/test_gpu_unavailable.sh` | PASS |
| `bash tests/test_startup_preflight.sh` | PASS |
| `python3 tests/test_migration.py` | PASS |
| Additional fault/edge-case reproductions | 16 reproduced |

Environment: Linux, Python 3.13.5, Bash 5.2, `jq` and Git. The local execution environment could not resolve GitHub DNS; repository access was through the authenticated GitHub connector, and the tested local source files were checked against its returned blob hashes. No RunPod credentials or GPUs were used.

**ShellCheck and Pyflakes were not available in this audit environment and were not independently rerun.** Their successful execution in the supplied external review remains a separate reported result. In addition to the unused `os` import in the backup script, `tests/test_watchdog.py` contains an unused `io` import; these are low-priority issues.

The reproductions in [`review/reproduce_20261027.py`](../../review/reproduce_20261027.py) deliberately assert the current defective behaviour. A passing reproduction means **the issue was demonstrated**, not that the implementation is correct. Keep them outside the normal regression gate; convert them into desired-behaviour regression tests during fixes.

## Priority findings

### R1 — High: a failed shutdown attempt terminates the watchdog

**Location:** `bin/bonsai-watchdog.py:125-135`.

The real-stop branch calls `subprocess.run(..., check=True, timeout=45)` without catching process failures. Missing executables, authorization failures, a nonzero command exit or a timeout can end the watchdog immediately. There is no stop retry, no reconciliation after an ambiguous timeout, and no verification that the Pod reached a stopped state. The current `nohup` launcher does not restart this process.

**Reproduced:** a mocked stop returning nonzero propagates `CalledProcessError`, and exactly one stop attempt occurs.

**Fix:** use one deliberately supported stop backend, preferably a shared REST client with bounded HTTP timeouts, explicit error classification, redacted logging, retry/backoff and status reconciliation. If the watchdog survives an accepted stop request, it should keep checking/retrying until the resource stops or an explicit failure policy applies. Do not treat HTTP acceptance as proof that billing stopped. Test timeout-after-acceptance, HTTP 401/403/429/5xx and malformed payloads.

Do not copy the controller's broad account-level `All` token into an autonomous Pod by default. Prefer a separately scoped credential where supported, and retain a provider/account-level spending guard. Replacing `runpodctl` with ten lines of HTTP code alone does not solve reliability or credential scope.

### R2 — High: the session ceiling resets when the watchdog restarts

**Location:** `bin/bonsai-watchdog.py:104-110`; `systemd/bonsai-watchdog.service` uses `Restart=on-failure`.

The eight-hour ceiling starts at each watchdog process's `time.monotonic()`. A restart therefore creates a fresh eight-hour allowance. Repeated process failures can defeat the intended session cost ceiling.

**Reproduced:** at the same simulated Pod age, the original watchdog reaches its hard deadline while a newly constructed watchdog does not.

**Fix:** persist a deadline tied to a specific Pod run/session, not just a process. Validate the provider's actual session timestamp field before relying on a proposed `lastStartedAt` property. A durable local record must distinguish watchdog restart from a genuinely new Pod session and handle clock changes. Keep monotonic time for within-process intervals.

### R3 — High: the server can start without its watchdog, and launch success is not readiness

**Locations:** `bin/bonsai-pod-services-start:13-44`; `bin/bonsai-server-start:11-21`.

The helper backgrounds the model server and immediately probes `/slots` once. A normal cold start returning HTTP 503 causes the helper to exit before launching the watchdog, while the server keeps running. A permanent model startup failure also prevents the watchdog from being created. In addition, the server launcher returns success and prints a PID even when its child exits immediately.

**Reproduced:** a simulated not-ready `/slots` leaves a started server and no watchdog PID; a fake server exiting with code 7 still yields a successful launcher exit.

**Fix:** start an independently supervised watchdog before/alongside the model, with a documented startup grace period and bounded readiness checks. Monitor health and the expected process identity. Distinguish `launched`, `loading`, `ready` and `failed`. Do not silently abandon a potentially billable Pod when readiness fails.

Neither `nohup` nor `setsid` is a process supervisor or boot integration. The exact RunPod template's hook still requires a live, controlled restart test.

### R4 — High: a backup creation race can delete another writer's completed archive

**Location:** `bin/bonsai-backup.py:58-85`.

The existence check and exclusive archive creation are separated. If another process creates the output after the check, `tarfile.open(..., 'x:gz')` raises. The broad exception handler nevertheless unlinks the output and manifest, even though this invocation did not create them.

**Reproduced:** a controlled concurrent-writer interleaving creates an archive just before the exclusive open fails; the exception handler deletes that archive.

**Fix:** create uniquely named temporary files, track ownership of each artifact, and remove only artifacts created by the current invocation. Publish archive and manifest with a no-clobber protocol and locking as appropriate. Add a two-writer regression test. This matters especially when the archive is intended to be the only recovery copy.

### R5 — High for migration: the backup does not include the expected runtime closure

**Location:** `bin/bonsai-backup.py:15-37`.

The optional projector allowlist names `mmproj-Bonsai-2-27B-f16.gguf`, whereas the model previously downloaded in this lab was `Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf`. The KV-bias asset used by the local KV4 setup is not selected. All symlinks are skipped, so a runtime bundle using SONAME/library symlink chains can lose required loader names even when the versioned library file is copied. The primary model is required, but a working CUDA runtime is not.

**Reproduced:** an otherwise valid fixture containing the actual projector filename, a KV-bias file and `libggml.so -> libggml.so.1` silently omits all three, retaining only the versioned regular library.

**Qualification:** the projector is optional for text-only inference and the cloud baseline did not establish a KV4 requirement. Symlink failure depends on the real runtime layout. This is not proof that the tested text-only cloud server would fail; it is proof that the current tool cannot support a complete-migration claim without inventory validation.

**Fix:** generate and review an explicit dependency inventory for the chosen model/runtime/settings. Preserve only safe in-root relative links, or deliberately materialize their targets and record that transformation. Verify actual shared-library dependencies and run an offline smoke check of the restored runtime before deleting the original data. Re-clone pinned source and download verified assets when reconstruction is simpler than copying a container workspace.

### R6 — Medium: backup confidentiality and source containment claims are too strong

**Location:** `bin/bonsai-backup.py:5-6, 20-36`.

The whole `bin/cuda` directory is accepted except for a few exact path-component names. Files such as `server.env`, `.env.production` or a private key with an unexpected name are not excluded. Checking only the final model path for symlink status does not reject a symlinked parent directory leading outside the source root.

**Reproduced:** a synthetic `bin/cuda/server.env` is selected; a model reached through a symlinked `models` directory outside the root is selected. No real secret was used or disclosed.

**Fix:** use an explicit artifact allowlist plus resolved-path containment checks, review the manifest, and create private outputs by default. Avoid an unconditional promise that secrets are never included: filename filtering alone cannot establish that.

### R7 — Medium: hashing and copying different file reads can produce an invalid successful backup

**Location:** `bin/bonsai-backup.py:73-80`.

A file is hashed, then opened and copied separately. A change between the two operations can make the recorded hash disagree with the archived bytes while backup reports completion.

**Reproduced:** a same-size synthetic model mutation after hashing results in a completed archive that the restore correctly rejects for checksum mismatch.

**Fix:** hash the same stream used to create the archive and verify a stable snapshot or detect source changes. Immutable model/runtime assets reduce this risk but do not remove the implementation defect.

### R8 — Medium: restore validates checksums but is not a transactional, symlink-safe restore

**Location:** `bin/bonsai-restore.py:18-20, 43-79`.

An existing destination symlink to an empty external directory is accepted and followed. Valid archive members `a` and `a/b` pass the preflight and hash checks; writing `a/b` then fails because `a` is a file, leaving partial output. A retry is refused because the destination is no longer empty. Canonical path aliases and disk/write failures need similar treatment.

**Reproduced:** output is written through a caller-supplied destination symlink; a file/directory collision leaves a partial tree that cannot be retried directly.

**Qualification:** this is not a demonstrated `../` archive traversal; those paths and archive symlinks are rejected. The destination-symlink case depends on the destination chosen by the caller or an untrusted local filesystem. Existing no-overwrite and hash safeguards are worth retaining.

**Fix:** validate canonical path names and file/directory conflicts; reject unintended destination/ancestor links; extract into a private staging tree with resource limits; verify final outputs; then publish without overwriting an existing destination. Clean only staging files owned by that invocation. A SHA-256 manifest proves consistency, not the authenticity of an untrusted archive-and-manifest pair.

### R9 — Medium: PID files do not establish ownership or idempotent process startup

**Locations:** `bin/bonsai-server-start:15-20`; `bin/bonsai-pod-services-start:19-21`.

`kill -0` only checks that a PID exists and can be signalled. Persistent `/workspace` PID files can refer to unrelated processes after a container restart. The check/start/write sequence also has no lock, so concurrent launches can race.

**Reproduced:** a PID file containing the audit process's PID causes the server launcher to return success without starting the fake server.

**Fix:** prefer a supervisor; otherwise lock startup and validate PID plus process start time, executable and session/container identity. Do not kill a process based solely on a stale PID file. Readiness must be checked separately.

### R10 — Medium: configuration is only partially propagated

| Location | Finding |
| --- | --- |
| `systemd/bonsai-watchdog.service:8` | Path lacks `/bin/`, unlike the current documented install location. |
| `bin/runpod-serve:25-29` | Remote launcher path hardcodes `/workspace/ai/Bonsai-demo`. |
| `bin/runpod-bonsai:62-63` | Health check ignores `RUNPOD_LOCAL_PORT` and uses 18082. |
| `bin/bonsai-pod-services-start:26-43` | Preflight uses `BONSAI_PORT`, but the watchdog is launched without `--url`, reverting to port 18080. |
| `config/*.env.example` | Personal `/home/maxd` path remains; the old tunnel example is not consumed by the current launcher. |

**Reproduced:** configuring local port 19082 still probes 18082; configuring the model port 19080 does not pass that port to the watchdog.

**Fix:** define and validate one shared configuration contract. Use safe defaults based on `$HOME`, a configurable remote root, and a consistently derived model URL. When building remote SSH commands, quote or validate the remote path; local argument arrays do not automatically make remote shell interpolation safe. Deprecate/remove the unused tunnel example and align installation docs.

### R11 — Medium: dynamic endpoint discovery occurs once, not on every autossh reconnect

**Location:** `bin/runpod-tunnel:29-51`; `systemd/runpod-tunnel.service`.

The wrapper resolves the endpoint and then `exec`s autossh. Autossh can keep its own process alive and restart SSH using the old endpoint. Systemd does not rerun the wrapper while autossh remains running, so a Pod that moves SSH IP/port can remain unreachable until the service is restarted.

**Evidence:** source/control-flow review; no real endpoint rotation was performed in this audit. The documented [autossh behaviour](https://manpages.debian.org/bookworm/autossh/autossh.1.en.html) is to restart the child SSH process.

**Fix:** make an orchestrator explicitly refresh endpoint data and restart the tunnel on Pod-session changes, or design a bounded reconnect loop that re-resolves after failure. Add connection timeouts and a fake-endpoint-change test. Preserve host-key checking and require explicit handling of unexpected key changes; never silently clear `known_hosts` on routine reconnect.

### R12 — Medium: secret-protection and diagnostic gaps remain

`.gitignore` does not match the actual key basename `runpod_ed25519`, migration archives, or their manifests. This does not prove any secret has been committed; the default key lives outside the repository. It increases the risk if an operator copies artifacts into the repository. **Reproduced** with `git check-ignore` and synthetic filenames.

The controller passes the bearer token in curl's argument list. The threat depends on local process visibility; it is not a claim of remote exposure. `runpod-inspect` omits `env` but prints arbitrary Docker `args`, which could themselves contain secrets. Action responses are printed wholesale. Review these paths rather than promising all diagnostic output is secret-free.

**Fix:** use explicit private config handling, avoid tokens in visible argv where feasible, redact output with an allowlist, add ignore patterns for the actual artifacts, and introduce secret scanning. A public repository audit should include history before asserting that no secret was ever committed; this review did not inspect all history.

## Additional correctness and maintenance notes

- The watchdog's idle timer starts from the last busy/failed observation rather than the first confirmed idle observation. A 60-second timeout can trigger after only 30 seconds of observed idleness with 30-second polling. This was reproduced; start the idle interval on the state transition. Polling `/slots` alone can also miss short requests between samples.
- `get_slot_state()` is called immediately before the stop decision in the current loop: there is **not an intentional extra 30-second sleep** between that probe and the stop command. A final recheck is useful but still cannot atomically prevent a new request arriving afterwards. Strong protection needs an admission/drain or lease policy; the hard session cap remains an explicitly interrupting policy.
- `start` confirmation is currently safe-by-default but needs an explicit `--yes` interface for automation, with EOF handled as cancellation. Do not remove the default prompt.
- Endpoint parsing should validate integer port values, canonical IP/host formats, response IDs and schema errors. Current error classification for GPU exhaustion is tied to one English message; retain the safe no-retry fallback for unknown errors.
- `runpod-bonsai up/down/wait` is a missing feature, not a hidden implemented guarantee. Define state transitions, bounded waits and partial-failure cleanup before adding it. A failed tunnel stop must not prevent a requested Pod stop.
- Dry-run exiting after its first would-stop decision is documented. `nohup` does not keep it observing afterwards; do not interpret the presence of a stale watchdog PID file as active protection.
- The startup-hook preflight is text matching, not shell parsing. It can treat a string or conditional code as a candidate. Its current manual-review warning is appropriate; do not promote it to automatic installation authorization.
- `BONSAI_PORT`, context and timeouts need meaningful range and finite-number validation. Reject NaN/infinite watchdog time settings.
- Runtime and watchdog logs need bounded retention. Renaming a currently open logfile alone does not make a writer reopen it.
- A common SSH helper would reduce duplicated endpoint/key/config handling in `runpod-tunnel` and `runpod-serve`.
- Add one offline test runner, CI and negative-path tests. The four existing test entrypoints are not four comprehensive tests: one runs eight unit tests, and the others are narrower smoke checks.
- Keep README focused, but retain a prominent warning that cost protection is not deployed. Fix literal `\\n\\n`, update stale status statements in the technical report, and state tested Python/Bash versions. Shell `mapfile` is not portable to the default Bash 3.2 on older macOS installations; the controller's documented Linux target is appropriate.
- The owner should choose a licence explicitly. This review does not add MIT/Apache terms on the owner's behalf. [GitHub's licensing guidance](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository) is the relevant reference.

## Clarifications on the supplied external review

1. Replacing `runpodctl` with REST is reasonable for consistency, not because `runpodctl pod stop` is inherently invalid: [current RunPod documentation](https://docs.runpod.io/runpodctl/reference/runpodctl-pod) documents that command. Validate the installed version or remove the dependency deliberately.
2. `nohup` already ignores hangup signals ([manual](https://manpages.debian.org/bookworm/coreutils/nohup.1.en.html)). `setsid` may be useful, but the core reliability gap is missing supervision, readiness and process identity, not simply absence of `setsid`.
3. The proposed final `/slots` check narrows a race; it is not a complete no-interruption guarantee.
4. A saved deadline needs a Pod-session identity. Persisting a timestamp without defining reset semantics can stop a genuinely new Pod run too early.
5. Passing lint and existing tests is valuable but not evidence of complete lifecycle or recovery safety. The independent fault reproductions materially change the priority order.

## Recommended change sequence

**PR 1 — Safety and regression coverage:** fix backup ownership/cleanup and exact artifact selection; restore transaction boundaries; process identity and readiness; watchdog stop failures and durable session deadline. Add desired-behaviour regression tests and keep real shutdown disabled until live validation.

**PR 2 — Shared configuration and portability:** configurable ports/root, corrected service path, one SSH configuration path, stale example cleanup, redacted diagnostics, explicit `--yes`, test runner and CI. Licence selection requires the owner's separate decision.

**PR 3 — Lifecycle orchestration:** implement `up/down/wait` with explicit state transitions, endpoint refresh, bounded readiness, independent watchdog supervision and visible partial-failure handling. No automatic replacement Pod or deletion of the original storage.

**Live acceptance gate:** with an explicit GPU spending window, test cold startup, server crash, watchdog restart, stop authorization failure, controller disconnect, endpoint changes, confirmed Pod stop, and an actual restored runtime. Unit tests cannot replace this gate.

## Reproduction summary

All 16 audit reproductions completed successfully against the reviewed snapshot. They demonstrate: stop failure propagation; session-cap reset; early idle accounting; omitted projector/bias/library link; secret-like file inclusion; source-parent symlink escape; concurrent backup cleanup deletion; hash/copy inconsistency; destination symlink following; partial restore on structural collision; stale-PID suppression; false-positive launch success; cold-start watchdog omission; missed custom watchdog port; ignored controller health port; and missing ignore rules.

All data was synthetic, all cloud operations were mocked, and filesystem effects were limited to temporary directories. **No real backup was deleted, no credential was used, and no GPU was started.**
