# RunPod Bonsai Lab

Use Bonsai 2 27B locally or on an existing RunPod GPU Pod, from a self-hosted
OpenAI-compatible client such as OpenCode. The controller runs on a Linux PC;
GPU model and hostname are deployment choices, not hardcoded requirements.

```text
Browser -> local Linux host / OpenCode
              |-- LOCAL -> local llama-server
              +-- CLOUD -> 127.0.0.1:18082 -> private SSH tunnel
                                               -> Pod 127.0.0.1:18080
```

Manual cloud inference has been demonstrated. Shared configuration/SSH, Pod-local
supervision and `up/down/wait` orchestration are implemented with offline tests.
A live provider/systemd/SSH acceptance cycle is still required. See [Status](docs/STATUS.md).

## Occasional use: Modal (serverless)

For a few hours a month, [Modal](docs/MODAL.md) is simpler than a Pod: the GPU
container scales to zero when idle, at most one runs at a time, and requests
need a Modal proxy token. There is no Pod, tunnel or watchdog to manage.

```bash
modal run cloud/bonsai_modal.py::download   # model -> Volume, CPU only
modal deploy cloud/bonsai_modal.py          # HTTPS endpoint, L40S by default
python3 cloud/smoke.py                      # cold start + tok/s
```

Running on Modal since 28 October 2026: L40S, ~12 s cold start, 72–76 tok/s. An OpenCode
session and scale-down are still to be confirmed; see the [acceptance checklist](docs/MODAL.md#live-acceptance-checklist).

## Quick start: controller

Requires Linux, Bash, Python 3.11+, Git and OpenSSH. `up` additionally requires a
working systemd user manager. Create a Pod separately and install its model, matching
Pod scripts and SSH public key before use. These commands do not start a GPU:

```bash
git clone https://github.com/MassimoDanieli/runpod-bonsai-lab.git
cd runpod-bonsai-lab
mkdir -p "$HOME/.local/bin" "$HOME/.config"
install -m 700 bin/runpod-client.py bin/runpod-lifecycle.py bin/runpod-bonsai bin/runpod-ssh bin/runpod-tunnel bin/runpod-serve bin/runpod-inspect "$HOME/.local/bin/"
test -e "$HOME/.config/runpod.env" || install -m 600 config/runpod-bonsai.env.example "$HOME/.config/runpod.env"
vim "$HOME/.config/runpod.env"
```

Set your existing Pod ID and API key. Keep existing credentials when updating;
install all wrappers with both sibling Python modules. Configuration is parsed
as data, not shell code. See [Configuration and SSH](docs/CONFIGURATION.md).

```bash
~/.local/bin/runpod-bonsai check-config  # local only
~/.local/bin/runpod-bonsai status
~/.local/bin/runpod-bonsai up           # asks before paid compute; prepares CLOUD
~/.local/bin/runpod-bonsai down         # local tunnel cleanup + Pod stop + EXITED check
~/.local/bin/runpod-bonsai wait --state EXITED --timeout 120  # read-only
```

[Lifecycle guide](docs/LIFECYCLE.md) explains prerequisites, deadlines, idempotent
retries, partial failures and the dedicated transient user service. `up --yes`
is explicit approval for paid compute. Low-level `start/stop/serve/tunnel` commands
remain available. GPU_UNAVAILABLE returns exit 3 and preserves the existing Pod;
there is no replacement-Pod creation or blind paid-action retry. A failed `up`
**does not automatically stop a potentially shared Pod**: inspect and use `down`.

## Pod processes

[Pod startup](docs/POD_STARTUP.md) describes `bonsai-pod.py start|status|stop|run`.
It manages separate model/watchdog processes, checks readiness, rejects stale PID
identities and preserves the local session deadline. Compatibility launchers start
that managed pair. It is not a verified boot hook; the supervised watchdog remains
dry-run and cannot stop GPU billing. A lifecycle-created session is a local request
record, not a verified provider allocation timestamp.

## Cost and safety

The reference RTX 5090 experiment displayed approximately **$1.20/hour** on
25 October 2026; this is an observed example, not a current price guarantee.
Stopped storage can remain billable and a stopped Pod may be unable to regain a GPU.
Always confirm EXITED after stopping; watchdog dry-run and local SSH stop do not stop billing.

Keep model APIs on loopback, using direct TCP SSH rather than a public HTTP proxy.
The shared tunnel refreshes mappings instead of reconnecting forever to an old
address. Host keys are never silently cleared; first-contact accept-new remains
trust on first use. Stop manual/legacy tunnels explicitly before first lifecycle use.
Docker clients need a route to the host endpoint; bridge-network loopback is not the host.
Keep credentials outside Git. Hashes prove integrity, not authenticity or a full Pod snapshot.

## Offline checks

```bash
bash tests/run-all.sh
```

The gate uses synthetic files, mocked cloud operations and loopback fake servers,
not GPU resources or real credentials. Restore/process tests require Linux;
CI checks ShellCheck, Pyflakes and network-free OpenSSH option parsing. Local and
CI results are recorded separately in each PR; mocked systemd is not live validation.

## Documentation

- [Status and pending work](docs/STATUS.md)
- [Existing-Pod up/down/wait lifecycle](docs/LIFECYCLE.md)
- [Shared configuration and SSH](docs/CONFIGURATION.md)
- [Dynamic SSH tunnel](docs/DYNAMIC_TUNNEL.md)
- [Pod supervision, startup and template preflight](docs/POD_STARTUP.md)
- [Watchdog, REST shutdown and session persistence](docs/WATCHDOG.md)
- [Controlled backup and restoration](docs/MIGRATION.md)
- [Original lab and indicative benchmarks](docs/TECHNICAL_REPORT.md)

LOCAL remains independent when cloud services are unavailable.
