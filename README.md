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

Manual end-to-end cloud inference has been demonstrated. Pod-local process
supervision is now implemented and tested offline; boot integration and unattended
shutdown still require live acceptance. See [Status](docs/STATUS.md).

## Quick start: controller

Requires Linux, Bash, Python 3.11+, Git, curl, jq and OpenSSH. For an optional managed
tunnel, install autossh/systemd. Create a RunPod Pod separately; these commands do
not create or start one. Set up the Pod model and its SSH public key before connecting.

```bash
git clone https://github.com/MassimoDanieli/runpod-bonsai-lab.git
cd runpod-bonsai-lab
mkdir -p "$HOME/.local/bin" "$HOME/.config"
install -m 700 bin/runpod-bonsai "$HOME/.local/bin/"
test -e "$HOME/.config/runpod.env" || install -m 600 config/runpod-bonsai.env.example "$HOME/.config/runpod.env"
vim "$HOME/.config/runpod.env"
```

Set your existing Pod ID and API key; adapt optional paths to your user account.
Do not overwrite an existing private configuration when updating the repository.

```bash
~/.local/bin/runpod-bonsai status
~/.local/bin/runpod-bonsai start   # asks before starting paid compute
~/.local/bin/runpod-bonsai stop   # confirm EXITED afterwards
```

A start request does not mean inference is ready. See the SSH/startup guides below.
The controller preserves the existing Pod and returns exit code 3 on
`GPU_UNAVAILABLE`; it does not automatically retry or create replacements.

## Pod processes

[Pod startup](docs/POD_STARTUP.md) describes `bonsai-pod.py start|status|stop|run`.
It manages separate model/watchdog processes, validates readiness, rejects stale PID
identities and retains the session deadline across local restarts. The compatibility
launchers now start that managed pair. This is **not** the cloud lifecycle controller,
a verified boot hook, or a GPU billing stop; the supervised watchdog stays dry-run.

## Cost and safety

The reference RTX 5090 experiment displayed approximately **$1.20/hour** on
25 October 2026; this is an observed example, not a current price guarantee.
Stopped Pod storage can remain billable, and a stopped Pod may be unable to regain
a GPU. Always confirm EXITED after stopping; watchdog **dry-run does not stop billing**.

Keep inference on loopback, using direct TCP SSH rather than a public HTTP proxy.
Docker clients need a way to reach the host endpoint; bridge-network `127.0.0.1`
is not the host. Keep credentials outside the repository and model workspace.
A SHA-256 manifest is an integrity check, not an authenticity signature or a full
Pod snapshot. Retain source data until restored inference is verified.

## Offline checks

```bash
bash tests/run-all.sh
```

The gate uses synthetic files, mocked cloud operations and loopback fake servers,
not GPU resources or real API credentials. Restore/process tests require Linux;
the supervisor uses `/proc`, `flock` and pidfds. CI also runs static checks;
see each PR for actual execution results and remaining limitations.

## Documentation

- [Status and remaining work](docs/STATUS.md)
- [Dynamic SSH tunnel](docs/DYNAMIC_TUNNEL.md)
- [Pod supervision, startup and template preflight](docs/POD_STARTUP.md)
- [Watchdog, REST shutdown and session persistence](docs/WATCHDOG.md)
- [Controlled artifact backup and restoration](docs/MIGRATION.md)
- [Original lab report and indicative benchmarks](docs/TECHNICAL_REPORT.md)

The local model remains independent when cloud services are unavailable.
