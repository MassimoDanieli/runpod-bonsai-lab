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

Manual cloud inference has been demonstrated. Shared configuration/SSH and Pod-local
process supervision are implemented and tested offline; live acceptance and full
lifecycle orchestration remain pending. See [Status](docs/STATUS.md).

## Quick start: controller

Requires Linux, Bash, Python 3.11+, Git and OpenSSH. Systemd is optional for managed
tunneling. Create a Pod separately and install its model/SSH public key before use.
The following six commands install tools and edit configuration, without starting a GPU:

```bash
git clone https://github.com/MassimoDanieli/runpod-bonsai-lab.git
cd runpod-bonsai-lab
mkdir -p "$HOME/.local/bin" "$HOME/.config"
install -m 700 bin/runpod-client.py bin/runpod-bonsai bin/runpod-ssh bin/runpod-tunnel bin/runpod-serve bin/runpod-inspect "$HOME/.local/bin/"
test -e "$HOME/.config/runpod.env" || install -m 600 config/runpod-bonsai.env.example "$HOME/.config/runpod.env"
vim "$HOME/.config/runpod.env"
```

Set your existing Pod ID and API key. Keep existing credentials when updating;
install all wrappers with their sibling `runpod-client.py`. Configuration is parsed
as data, not shell code. See [Configuration and SSH](docs/CONFIGURATION.md) for the
supported variables, private file permissions and host-key migration requirements.

```bash
~/.local/bin/runpod-bonsai check-config  # local only
~/.local/bin/runpod-bonsai status
~/.local/bin/runpod-bonsai start         # asks before paid compute; --yes explicitly opts in
~/.local/bin/runpod-bonsai stop          # then confirm EXITED with status
```

A start request is not inference readiness. SSH/serving/tunnel commands are separate;
`up/down/wait` is not implemented. GPU_UNAVAILABLE returns exit 3 and preserves the
existing Pod. No automatic action retry or replacement-Pod creation occurs.

## Pod processes

[Pod startup](docs/POD_STARTUP.md) describes `bonsai-pod.py start|status|stop|run`.
It manages separate model/watchdog processes, checks readiness, rejects stale PID
identities and preserves the local session deadline. Compatibility launchers start
that managed pair. It is not a verified boot hook or cloud lifecycle controller;
the supervised watchdog remains dry-run and cannot stop GPU billing.

## Cost and safety

The reference RTX 5090 experiment displayed approximately **$1.20/hour** on
25 October 2026; this is an observed example, not a current price guarantee.
Stopped storage can remain billable and a stopped Pod may be unable to regain a GPU.
Always confirm EXITED after stopping; watchdog dry-run and local SSH stop do not stop billing.

Keep model APIs on loopback, using direct TCP SSH rather than a public HTTP proxy.
The new tunnel re-discovers mappings through a shared OpenSSH supervisor instead of
letting autossh reconnect forever to an old address. Host keys are never silently
cleared; first-contact accept-new remains trust on first use, not independent verification.
Docker clients need a route to the host endpoint; bridge-network loopback is not the host.
Keep credentials outside Git. Hashes prove integrity, not authenticity or a full Pod snapshot.

## Offline checks

```bash
bash tests/run-all.sh
```

The gate uses synthetic files, mocked cloud operations and loopback fake servers,
not GPU resources or real credentials. Install jq for the retained test fixtures.
Restore/process tests require Linux; CI also checks ShellCheck, Pyflakes and a
network-free OpenSSH option-parsing test. Results and limits are recorded in each PR.

## Documentation

- [Status and pending work](docs/STATUS.md)
- [Shared configuration and SSH](docs/CONFIGURATION.md)
- [Dynamic SSH tunnel](docs/DYNAMIC_TUNNEL.md)
- [Pod supervision, startup and template preflight](docs/POD_STARTUP.md)
- [Watchdog, REST shutdown and session persistence](docs/WATCHDOG.md)
- [Controlled backup and restoration](docs/MIGRATION.md)
- [Original lab and indicative benchmarks](docs/TECHNICAL_REPORT.md)

LOCAL remains independent when cloud services are unavailable.
